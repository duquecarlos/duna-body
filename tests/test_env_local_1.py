"""body.entorno.cargar(): the local .env loader (BODY-ENV-LOCAL-1)."""
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from body import entorno, web  # noqa: F401  (import used only to confirm module caching below)


class CargarTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="body-env-test-")

    def _ruta(self, contenido: str) -> Path:
        ruta = Path(self._dir) / ".env"
        ruta.write_text(contenido, encoding="utf-8")
        return ruta

    def test_carga_una_linea_body_agente(self):
        ruta = self._ruta("BODY_AGENTE_ANTHROPIC_API_KEY=sk-fake\n")
        entorno_fake = {}
        cargados, ignorados = entorno.cargar(ruta, entorno_fake)
        self.assertEqual((cargados, ignorados), (1, 0))
        self.assertEqual(entorno_fake["BODY_AGENTE_ANTHROPIC_API_KEY"], "sk-fake")

    def test_ignora_y_cuenta_nombres_fuera_del_prefijo(self):
        ruta = self._ruta(
            "BODY_CUENTA_ISSUER=https://cuenta.duna.solutions\n"
            "BODY_HOME=C:\\otra\\ruta\n"
            "PATH=C:\\Windows\n"
            "body_agente_minuscula=x\n"
        )
        entorno_fake = {}
        cargados, ignorados = entorno.cargar(ruta, entorno_fake)
        self.assertEqual((cargados, ignorados), (0, 4))
        self.assertEqual(entorno_fake, {})

    def test_no_sobreescribe_nombre_ya_presente(self):
        ruta = self._ruta("BODY_AGENTE_ANTHROPIC_API_KEY=sk-del-archivo\n")
        entorno_fake = {"BODY_AGENTE_ANTHROPIC_API_KEY": "sk-real"}
        cargados, ignorados = entorno.cargar(ruta, entorno_fake)
        self.assertEqual((cargados, ignorados), (0, 0))
        self.assertEqual(entorno_fake["BODY_AGENTE_ANTHROPIC_API_KEY"], "sk-real")

    def test_quita_comillas(self):
        ruta = self._ruta(
            'BODY_AGENTE_ANTHROPIC_API_KEY="sk-con-comillas"\n'
            "BODY_AGENTE_MODELO_ANTHROPIC='claude-sonnet-5-5'\n"
        )
        entorno_fake = {}
        entorno.cargar(ruta, entorno_fake)
        self.assertEqual(entorno_fake["BODY_AGENTE_ANTHROPIC_API_KEY"], "sk-con-comillas")
        self.assertEqual(entorno_fake["BODY_AGENTE_MODELO_ANTHROPIC"], "claude-sonnet-5-5")

    def test_prefijo_export(self):
        ruta = self._ruta("export BODY_AGENTE_ANTHROPIC_API_KEY=sk-export\n")
        entorno_fake = {}
        cargados, _ = entorno.cargar(ruta, entorno_fake)
        self.assertEqual(cargados, 1)
        self.assertEqual(entorno_fake["BODY_AGENTE_ANTHROPIC_API_KEY"], "sk-export")

    def test_tolera_bom(self):
        ruta = Path(self._dir) / ".env"
        ruta.write_bytes("\ufeffBODY_AGENTE_ANTHROPIC_API_KEY=sk-bom\n".encode("utf-8"))
        entorno_fake = {}
        cargados, _ = entorno.cargar(ruta, entorno_fake)
        self.assertEqual(cargados, 1)
        self.assertEqual(entorno_fake["BODY_AGENTE_ANTHROPIC_API_KEY"], "sk-bom")

    def test_salta_comentarios_y_lineas_vacias(self):
        ruta = self._ruta(
            "# comentario\n"
            "\n"
            "BODY_AGENTE_ANTHROPIC_API_KEY=sk-ok\n"
        )
        entorno_fake = {}
        cargados, ignorados = entorno.cargar(ruta, entorno_fake)
        self.assertEqual((cargados, ignorados), (1, 0))

    def test_valor_vacio_no_se_carga(self):
        ruta = self._ruta("BODY_AGENTE_OPENAI_API_KEY=\n")
        entorno_fake = {}
        cargados, ignorados = entorno.cargar(ruta, entorno_fake)
        self.assertEqual((cargados, ignorados), (0, 0))
        self.assertNotIn("BODY_AGENTE_OPENAI_API_KEY", entorno_fake)

    def test_archivo_ausente(self):
        ruta = Path(self._dir) / "no-existe" / ".env"
        self.assertEqual(entorno.cargar(ruta, {}), (0, 0))

    def test_ruta_defecto_es_la_del_repo(self):
        raiz_repo = Path(__file__).resolve().parents[1]
        self.assertEqual(entorno.RUTA_DEFECTO, raiz_repo / ".env")

    def test_body_env_file_gana_sobre_ruta_defecto(self):
        """BODY-LIVE-CHECKOUT-1: the live checkout has no .env of its own; body.vivo sets
        BODY_ENV_FILE to the main repo's .env and cargar() must prefer it."""
        otra_ruta = self._ruta("BODY_AGENTE_ANTHROPIC_API_KEY=sk-vivo\n")
        entorno_fake = {"BODY_ENV_FILE": str(otra_ruta)}
        cargados, ignorados = entorno.cargar(None, entorno_fake)
        self.assertEqual((cargados, ignorados), (1, 0))
        self.assertEqual(entorno_fake["BODY_AGENTE_ANTHROPIC_API_KEY"], "sk-vivo")


class ImportarWebTest(unittest.TestCase):
    def test_importar_body_web_no_llama_cargar(self):
        """A fresh import of body.web must not call entorno.cargar() -- it is only called inside
        main(). Imports into a throwaway module object (popped and restored in sys.modules) rather
        than reloading the real one in place, so this test never disturbs the `body.web` module
        every other test module already holds a reference to.

        BODY-TESTS-VELOCIDAD-2 root-cause fix: the re-import repoints the `body` package's own
        `web` attribute (`sys.modules["body"].web`) at the throwaway module too -- a plain
        `from body import web` / `import body.web as x` resolves through that attribute, not
        through sys.modules, so it must be saved and restored alongside the sys.modules entry.
        _AUSENTE distinguishes "was absent before" from "was None before" so the restore never
        writes a None placeholder into either slot."""
        _AUSENTE = object()
        paquete = sys.modules["body"]
        modulo_original = sys.modules.get("body.web", _AUSENTE)
        atributo_original = getattr(paquete, "web", _AUSENTE)
        if modulo_original is not _AUSENTE:
            del sys.modules["body.web"]
        try:
            with patch("body.entorno.cargar") as cargar_mock:
                importlib.import_module("body.web")
            cargar_mock.assert_not_called()
        finally:
            if modulo_original is _AUSENTE:
                sys.modules.pop("body.web", None)
            else:
                sys.modules["body.web"] = modulo_original
            if atributo_original is _AUSENTE:
                if hasattr(paquete, "web"):
                    del paquete.web
            else:
                paquete.web = atributo_original


if __name__ == "__main__":
    unittest.main()
