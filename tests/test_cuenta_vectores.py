"""BODY-CUENTA-RP-1: runs body/cuenta.py's pure validator over EVERY vector in Cuenta's own shared
file (tests/fixtures/cuenta_vectores.json, copied verbatim from dev-protocol's
contracts/cuenta-duna-v1/vectores.json). Mirrors the loop in dev-protocol's own
contracts/cuenta-duna-v1/verificar_vectores.py (read the same way, via `git show`, for this slice).
No network; no real health data.
"""
import datetime as dt
import json
import unittest
import unittest.mock as mock
from pathlib import Path

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import cuenta

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cuenta_vectores.json"


def _load_vectors() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _verify_one(v: dict, caso: dict, jti_vistos: set) -> str | None:
    """Freezes PyJWT's clock to the vector's `ahora`, exactly like the reference verifier, then
    returns the `codigo` body/cuenta.py's validator produced (None on acceptance). `jti_vistos` is
    threaded across every caso in the file, like the reference verifier's own `vistos` set, so the
    `logout-jti-repetido` vector (a replay of `logout-con-sid`'s jti) is refused."""
    with mock.patch("jwt.api_jwt.datetime") as d:
        d.now.return_value = dt.datetime.fromtimestamp(caso["ahora"], tz=dt.timezone.utc)
        d.fromtimestamp = dt.datetime.fromtimestamp
        d.timezone = dt.timezone
        _, codigo = cuenta.verificar_claims(
            caso["token"],
            client_id=caso["cliente_id"],
            issuer=v["emisor"],
            ahora=caso["ahora"],
            margen=v["margen_segundos"],
            tipo=caso["tipo"],
            jwks=v["jwks"],
            jwks_tras_refetch=v.get("jwks_tras_refetch"),
            nonce_esperado=caso.get("nonce_esperado"),
            requiere_acr=caso.get("requiere_acr"),
            jti_vistos=jti_vistos,
        )
    return codigo


class CuentaVectorsTest(unittest.TestCase):
    def test_every_vector_matches_its_expected_result(self):
        v = _load_vectors()
        self.assertEqual(v["contrato"], "cuenta-duna-v1")
        casos = v["casos"]
        self.assertTrue(casos)
        failures = []
        jti_vistos: set = set()
        for caso in casos:
            got = _verify_one(v, caso, jti_vistos)
            want = caso["codigo"] if caso["esperado"] == "rechazar" else None
            if got != want:
                failures.append(f"{caso['id']}: got {got!r}, want {want!r}")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_vector_count_matches_the_copied_fixture(self):
        # Guards against a silently truncated copy of dev-protocol's vectors.json.
        v = _load_vectors()
        self.assertEqual(len(v["casos"]), 30)


if __name__ == "__main__":
    unittest.main()
