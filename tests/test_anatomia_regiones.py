"""BODY-AVATAR-3D-REGIONES-1: ONE canonical list of body regions (ruling 1), every other
vocabulary mapped into it both ways (ruling 2), and anatomia.contexto()'s own seam -- a
named-region state map, the same across every view, built from the same sets that already feed
the per-view glow/limitada/pendiente ellipse lists (ruling 3).
"""
import unittest

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import anatomia, db


def _T():
    from body.i18n import T
    return T("es")


REGIONES_CLAVES = frozenset(r["clave"] for r in anatomia.REGIONES)


class RegionesCanonicasTest(unittest.TestCase):
    """Ruling 1: REGIONES is exactly BRILLOS' own 12 groups, BRILLOS order, front then back."""

    def test_regiones_has_twelve_entries_matching_brillos(self):
        self.assertEqual(len(anatomia.REGIONES), 12)
        self.assertEqual(REGIONES_CLAVES,
                          frozenset(anatomia.BRILLOS["frente"]) | frozenset(anatomia.BRILLOS["espalda"]))

    def test_regiones_order_is_brillos_order_front_then_back(self):
        self.assertEqual([r["clave"] for r in anatomia.REGIONES],
                          list(anatomia.BRILLOS["frente"]) + list(anatomia.BRILLOS["espalda"]))

    def test_every_region_names_a_tipo_and_a_lado(self):
        for r in anatomia.REGIONES:
            self.assertIn(r["tipo"], ("musculo", "organo"))
            self.assertIn(r["lado"], ("frente", "espalda"))

    def test_corazon_and_cerebro_are_organo_frente(self):
        por_clave = {r["clave"]: r for r in anatomia.REGIONES}
        self.assertEqual(por_clave["corazon"]["tipo"], "organo")
        self.assertEqual(por_clave["corazon"]["lado"], "frente")
        self.assertEqual(por_clave["cerebro"]["tipo"], "organo")
        self.assertEqual(por_clave["cerebro"]["lado"], "frente")

    def test_every_other_region_is_musculo(self):
        por_clave = {r["clave"]: r for r in anatomia.REGIONES}
        for clave in REGIONES_CLAVES - {"corazon", "cerebro"}:
            self.assertEqual(por_clave[clave]["tipo"], "musculo")

    def test_brillos_frente_regions_are_lado_frente(self):
        por_clave = {r["clave"]: r for r in anatomia.REGIONES}
        for clave in anatomia.BRILLOS["frente"]:
            self.assertEqual(por_clave[clave]["lado"], "frente")

    def test_brillos_espalda_regions_are_lado_espalda(self):
        por_clave = {r["clave"]: r for r in anatomia.REGIONES}
        for clave in anatomia.BRILLOS["espalda"]:
            self.assertEqual(por_clave[clave]["lado"], "espalda")

    def test_every_musculo_or_organo_in_regiones_has_at_least_one_ellipse_in_brillos(self):
        for r in anatomia.REGIONES:
            disponibles = anatomia.BRILLOS[r["lado"]]
            self.assertIn(r["clave"], disponibles)
            self.assertGreaterEqual(len(disponibles[r["clave"]]), 1)


class CategoriaAGrupoExhaustivoTest(unittest.TestCase):
    """Ruling 2, training categoria -> regions (CATEGORIA_GRUPOS as it is): exhaustive both ways
    over CATEGORIA_ENUM, cardio/movilidad/otro asserted empty explicitly, never by omission."""

    def test_every_categoria_in_enum_has_an_explicit_reading_mapping_or_not(self):
        for categoria in anatomia.CATEGORIA_ENUM:
            grupos = anatomia.CATEGORIA_GRUPOS.get(categoria, frozenset())
            self.assertIsInstance(grupos, frozenset)

    def test_cardio_movilidad_otro_map_to_nothing_explicitly(self):
        for sin_grupo in ("cardio", "movilidad", "otro"):
            self.assertNotIn(sin_grupo, anatomia.CATEGORIA_GRUPOS)
            self.assertEqual(anatomia.CATEGORIA_GRUPOS.get(sin_grupo, frozenset()), frozenset())

    def test_every_categoria_grupos_key_is_in_the_enum(self):
        for categoria in anatomia.CATEGORIA_GRUPOS:
            self.assertIn(categoria, anatomia.CATEGORIA_ENUM)

    def test_every_categoria_grupos_value_names_only_real_regions(self):
        for categoria, grupos in anatomia.CATEGORIA_GRUPOS.items():
            for g in grupos:
                self.assertIn(g, REGIONES_CLAVES, f"{categoria!r} names unknown region {g!r}")

    def test_every_categoria_enum_value_covered_by_categoria_grupos_lookup(self):
        # Two-way: nothing in the enum is silently skipped by the test itself.
        covered = set(anatomia.CATEGORIA_GRUPOS) | {"cardio", "movilidad", "otro"}
        self.assertEqual(covered, set(anatomia.CATEGORIA_ENUM))


class DeAgujetasExhaustivoTest(unittest.TestCase):
    """Ruling 2, soreness keys (BODY-AGUJETAS-1) -> regions: DE_AGUJETAS, exhaustive both ways
    against body/db.py's own soreness keys -- grepped, never retyped from the spec sentence."""

    SORENESS_KEYS = frozenset(k for k in db.ENTEROS_MEDIDA_PERSONAL)

    def test_de_agujetas_keys_are_exactly_the_soreness_keys(self):
        self.assertEqual(frozenset(anatomia.DE_AGUJETAS), self.SORENESS_KEYS)

    def test_every_soreness_key_is_in_de_agujetas(self):
        for key in self.SORENESS_KEYS:
            self.assertIn(key, anatomia.DE_AGUJETAS)

    def test_de_agujetas_values_match_the_rulings_mapping(self):
        self.assertEqual(anatomia.DE_AGUJETAS["soreness_legs"], ("cuadriceps", "isquios"))
        self.assertEqual(anatomia.DE_AGUJETAS["soreness_glutes"], ("gluteos",))
        self.assertEqual(anatomia.DE_AGUJETAS["soreness_back"],
                          ("dorsales", "espalda_alta", "deltoides_post"))
        self.assertEqual(anatomia.DE_AGUJETAS["soreness_chest"], ("pecho",))
        self.assertEqual(anatomia.DE_AGUJETAS["soreness_shoulders"], ("hombros",))
        self.assertEqual(anatomia.DE_AGUJETAS["soreness_arms"], ("biceps",))
        self.assertEqual(anatomia.DE_AGUJETAS["soreness_core"], ("abdomen",))

    def test_every_de_agujetas_value_names_only_real_regions(self):
        for key, grupos in anatomia.DE_AGUJETAS.items():
            for g in grupos:
                self.assertIn(g, REGIONES_CLAVES, f"{key!r} names unknown region {g!r}")

    def test_an_unknown_soreness_key_fails_loudly(self):
        self.assertNotIn("soreness_unknown", anatomia.DE_AGUJETAS)
        with self.assertRaises(KeyError):
            anatomia.DE_AGUJETAS["soreness_unknown"]


class RegionesEstadoPrecedenceTest(unittest.TestCase):
    """Ruling 3: regiones_estado() -- one state per region, limitada > entrenado > pendiente."""

    def test_no_sets_gives_an_empty_map(self):
        self.assertEqual(anatomia.regiones_estado(), {})

    def test_entrenado_only(self):
        got = anatomia.regiones_estado(frozenset({"pecho", "hombros"}))
        self.assertEqual(got, {"pecho": "entrenado", "hombros": "entrenado"})

    def test_pendiente_only(self):
        got = anatomia.regiones_estado(grupos_pendientes=frozenset({"abdomen"}))
        self.assertEqual(got, {"abdomen": "pendiente"})

    def test_limitada_only(self):
        got = anatomia.regiones_estado(grupos_limitados=frozenset({"cuadriceps"}))
        self.assertEqual(got, {"cuadriceps": "limitada"})

    def test_limitada_wins_over_entrenado(self):
        got = anatomia.regiones_estado(grupos_lit=frozenset({"pecho"}),
                                        grupos_limitados=frozenset({"pecho"}))
        self.assertEqual(got, {"pecho": "limitada"})

    def test_entrenado_wins_over_pendiente(self):
        got = anatomia.regiones_estado(grupos_lit=frozenset({"gluteos"}),
                                        grupos_pendientes=frozenset({"gluteos"}))
        self.assertEqual(got, {"gluteos": "entrenado"})

    def test_limitada_wins_over_both(self):
        got = anatomia.regiones_estado(grupos_lit=frozenset({"isquios"}),
                                        grupos_limitados=frozenset({"isquios"}),
                                        grupos_pendientes=frozenset({"isquios"}))
        self.assertEqual(got, {"isquios": "limitada"})

    def test_result_follows_regiones_order(self):
        got = anatomia.regiones_estado(frozenset({"isquios", "pecho", "corazon"}))
        self.assertEqual(list(got), ["pecho", "corazon", "isquios"])

    def test_an_unknown_region_in_the_input_sets_is_simply_not_reported(self):
        # grupos_lit/limitados/pendientes come from CATEGORIA_GRUPOS/DE_AGUJETAS, already
        # restricted to real regions -- an unexpected key here is never promoted to a state.
        got = anatomia.regiones_estado(frozenset({"no_es_una_region"}))
        self.assertEqual(got, {})


class RegionesEstadoModeloSeamTest(unittest.TestCase):
    """BODY-CUERPO-ESTADO-MUSCULO-1 ruling 6: the widened seam -- estados_modelo wins over
    entrenado/pendiente, loses to limitada, never touches a region it has no entry for, and None
    (the default) keeps regiones_estado byte-for-byte what it was before this seam existed."""

    def test_no_model_given_is_byte_for_byte_the_old_behaviour(self):
        sin = anatomia.regiones_estado(frozenset({"pecho"}), frozenset(), frozenset({"abdomen"}))
        con_none = anatomia.regiones_estado(frozenset({"pecho"}), frozenset(), frozenset({"abdomen"}),
                                             estados_modelo=None)
        self.assertEqual(sin, con_none)

    def test_model_state_wins_over_entrenado(self):
        got = anatomia.regiones_estado(grupos_lit=frozenset({"pecho"}),
                                        estados_modelo={"pecho": "subentrenado"})
        self.assertEqual(got, {"pecho": "subentrenado"})

    def test_model_state_wins_over_pendiente(self):
        got = anatomia.regiones_estado(grupos_pendientes=frozenset({"gluteos"}),
                                        estados_modelo={"gluteos": "en_rango"})
        self.assertEqual(got, {"gluteos": "en_rango"})

    def test_limitada_still_wins_over_the_model(self):
        got = anatomia.regiones_estado(grupos_limitados=frozenset({"cuadriceps"}),
                                        estados_modelo={"cuadriceps": "sobrecargado"})
        self.assertEqual(got, {"cuadriceps": "limitada"})

    def test_organs_keep_entrenado_and_pendiente_when_the_model_has_no_entry(self):
        got = anatomia.regiones_estado(grupos_lit=frozenset({"corazon"}),
                                        grupos_pendientes=frozenset({"cerebro"}),
                                        estados_modelo={"pecho": "sin_datos"})
        self.assertEqual(got["corazon"], "entrenado")
        self.assertEqual(got["cerebro"], "pendiente")

    def test_a_region_absent_from_every_set_and_the_model_is_not_reported(self):
        got = anatomia.regiones_estado(estados_modelo={})
        self.assertEqual(got, {})


class ContextoRegionesSeamTest(unittest.TestCase):
    """Ruling 3: contexto()'s own `regiones` key -- the whole screen's state, unfiltered by any
    view's own permitidos, the same dict on every view in the list, equal to what the ellipse
    lists imply for a demo case. Ruling 3's last sentence: the existing glow/limitada/pendiente
    lists are untouched by this."""

    def test_regiones_matches_the_demo_cases_ellipse_lists(self):
        grupos_lit = anatomia.CATEGORIA_GRUPOS["pierna"]          # cuadriceps, isquios
        grupos_limitados = anatomia.CATEGORIA_GRUPOS["hombro"]    # hombros
        grupos_pendientes = anatomia.CATEGORIA_GRUPOS["gluteo"]   # gluteos
        ctx = anatomia.contexto(_T(), "hoy", grupos_lit, grupos_limitados, grupos_pendientes)
        esperado = {"hombros": "limitada", "cuadriceps": "entrenado", "isquios": "entrenado",
                    "gluteos": "pendiente"}
        for v in ctx:
            self.assertEqual(v["regiones"], esperado, f"view {v['id']!r} disagrees")

    def test_regiones_is_not_filtered_per_view_unlike_glow(self):
        # "corazon" view (hoy) only permits its own group (ruling: VISTA_GRUPOS) -- its glow list
        # stays empty for a leg category, but regiones still names every region that has a state.
        grupos_lit = anatomia.CATEGORIA_GRUPOS["pierna"]
        ctx = anatomia.contexto(_T(), "hoy", grupos_lit)
        corazon = next(v for v in ctx if v["id"] == "corazon")
        self.assertEqual(corazon["glow"], [])
        self.assertEqual(corazon["regiones"], {"cuadriceps": "entrenado", "isquios": "entrenado"})

    def test_no_training_or_lesion_or_plan_gives_an_empty_regiones_map(self):
        ctx = anatomia.contexto(_T(), "turnos", frozenset({"cerebro"}))
        for v in ctx:
            self.assertEqual(v["regiones"], {"cerebro": "entrenado"})

    def test_existing_glow_limitada_pendiente_lists_are_unchanged_by_this_slice(self):
        grupos = anatomia.CATEGORIA_GRUPOS["pierna"]
        ctx = anatomia.contexto(_T(), "hoy", grupos)
        musculos = next(v for v in ctx if v["id"] == "musculos")
        espalda = next(v for v in ctx if v["id"] == "espalda")
        self.assertEqual(len(musculos["glow"]), 2)
        self.assertEqual(len(espalda["glow"]), 2)


class ContextoVistasUnaffectedByModeloTest(unittest.TestCase):
    """Ruling 6's last sentence: `estados_modelo` only ever changes the hidden `regiones` map --
    the per-view glow/limitada/pendiente lists, labels, alts and every other 2D field Hoy's own
    `_cuerpo_hoy_ctx` builds its vistas from are identical with or without it."""

    def test_every_field_but_regiones_is_identical_with_and_without_a_model(self):
        grupos_lit = anatomia.CATEGORIA_GRUPOS["pierna"]
        grupos_pendientes = anatomia.CATEGORIA_GRUPOS["gluteo"]
        sin_modelo = anatomia.contexto(_T(), "hoy", grupos_lit, frozenset(), grupos_pendientes)
        con_modelo = anatomia.contexto(_T(), "hoy", grupos_lit, frozenset(), grupos_pendientes,
                                        estados_modelo={"cuadriceps": "sobrecargado"})
        self.assertEqual(len(sin_modelo), len(con_modelo))
        for a, b in zip(sin_modelo, con_modelo):
            for campo in ("id", "imagen", "glow", "limitada", "pendiente", "nitida", "label", "alt"):
                self.assertEqual(a[campo], b[campo], campo)
        self.assertNotEqual(sin_modelo[0]["regiones"], con_modelo[0]["regiones"])


if __name__ == "__main__":
    unittest.main()
