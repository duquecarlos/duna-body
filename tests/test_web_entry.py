"""body.web._ensure_streams and main(): pythonw has no console, so sys.stdout/sys.stderr are None there."""
import sys
import unittest
from unittest.mock import patch

from tests._isolate import TMP  # noqa: F401  (must come before any body import)

from tests._rutas import rutas_planas

from body import web


class EnsureStreamsTest(unittest.TestCase):
    def test_replaces_missing_streams(self):
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = None
        sys.stderr = None
        try:
            web._ensure_streams()
            self.assertIsNotNone(sys.stdout)
            self.assertIsNotNone(sys.stderr)
            sys.stdout.write("")
            sys.stderr.write("")
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    def test_leaves_present_streams_untouched(self):
        marker_out, marker_err = sys.stdout, sys.stderr
        web._ensure_streams()
        self.assertIs(sys.stdout, marker_out)
        self.assertIs(sys.stderr, marker_err)


class RouteSetTest(unittest.TestCase):
    """BODY-WEB-ESTRUCTURA-1: moving /ayuda and /healthz into body/rutas/ayuda.py must not add, drop
    or rename a single route. This (method, path) set was captured from `main` before that move --
    FastAPI 0.162 keeps an included router as one lazy `_IncludedRouter` wrapper in app.routes rather
    than flattening it eagerly, so tests/_rutas.py's rutas_planas() descends into it the same way for
    both sides."""

    BEFORE = {
        ("/", ("GET",)),
        ("/ajustes/asistente", ("GET",)),
        ("/ajustes/asistente", ("POST",)),
        ("/ajustes/dispositivos", ("GET",)),
        ("/ajustes/dispositivos", ("POST",)),
        ("/ajustes/dispositivos/{dispositivo_id}/revocar", ("POST",)),
        ("/api/asistente/mcp/herramientas", ("GET",)),  # BODY-MCP-REMOTO-SERVIDOR-1
        ("/api/asistente/mcp/llamar", ("POST",)),  # BODY-MCP-REMOTO-SERVIDOR-1
        ("/api/desplazamiento", ("POST",)),  # ORCH-CARLOS-DELEGATES-TIER-1-1
        ("/api/hogar/manana", ("GET",)),  # BODY-SERVICIO-HOGAR-1
        ("/api/hogar/serie", ("GET",)),  # BODY-SERVICIO-HOGAR-1
        ("/api/hogar/turnos", ("GET",)),  # BODY-HOGAR-TURNOS-1
        ("/api/hogar/herramientas", ("GET",)),  # BODY-HOGAR-ESCRIBE-2
        ("/api/hogar/registrar", ("POST",)),  # BODY-HOGAR-ESCRIBE-2
        ("/api/turnos", ("POST",)),
        ("/api/vida/semana", ("GET",)),
        ("/asistente/conversacion/{conversacion_id}", ("GET",)),
        ("/asistente/preguntar", ("POST",)),
        ("/asistente/propuestas/{propuesta_id}/aplicar", ("POST",)),
        ("/asistente/propuestas/{propuesta_id}/descartar", ("POST",)),
        ("/asistente/propuestas/{propuesta_id}/deshacer", ("POST",)),
        ("/ayuda", ("GET",)),
        ("/comida-compartida/{porcion_id}/aplicar", ("POST",)),  # BODY-COMIDA-COMPARTIDA-1
        ("/comida-compartida/{porcion_id}/descartar", ("POST",)),  # BODY-COMIDA-COMPARTIDA-1
        ("/comida/productos", ("GET",)),  # BODY-COMIDA-PRODUCTO-1
        ("/comida/productos/agregar", ("GET",)),  # BODY-COMIDA-PRODUCTO-1
        ("/comida/productos/guardar", ("POST",)),  # BODY-COMIDA-PRODUCTO-1
        ("/comida/productos/nuevo", ("POST",)),  # BODY-COMIDA-PRODUCTO-1
        ("/day/{day}", ("GET",)),
        ("/day/{day}/entreno/{source_id}", ("GET",)),  # BODY-ENTRENO-VISTA-SESION-1
        ("/day/{day}/noche-inusual/visto", ("POST",)),  # BODY-NOCHE-INUSUAL-1
        ("/day/{day}/noche-inusual/nota", ("POST",)),  # BODY-MOCK-R2-HOY-1
        ("/day/{day}/entreno/{source_id}/separar", ("POST",)),  # BODY-ENTRENO-VINCULO-1
        ("/day/{day}/entreno/unir", ("POST",)),  # BODY-ENTRENO-VINCULO-1
        ("/day/{day}/entreno/propuestas/{propuesta_id}/aplicar", ("POST",)),  # BODY-ENTRENO-VINCULO-1
        ("/day/{day}/entreno/propuestas/{propuesta_id}/descartar", ("POST",)),  # BODY-ENTRENO-VINCULO-1
        ("/day/{day}/entreno/propuestas/{propuesta_id}/deshacer", ("POST",)),  # BODY-ENTRENO-VINCULO-1
        ("/day/{day}/entreno/{source_id}/dolor", ("POST",)),  # BODY-PLAN-FASES-5
        ("/day/{day}/entreno/{source_id}/checkin", ("POST",)),  # BODY-PARTIDO-2
        ("/day/{day}/dolor-manana", ("POST",)),  # BODY-PLAN-FASES-5
        ("/day/{day}/despertar/proponer", ("POST",)),  # BODY-DESPERTAR-REAL-7
        ("/day/{day}/despertar/deshacer", ("POST",)),  # BODY-DESPERTAR-REAL-7
        ("/day/{day}/despertar/propuestas/{propuesta_id}/aplicar", ("POST",)),  # BODY-DESPERTAR-REAL-7
        ("/day/{day}/despertar/propuestas/{propuesta_id}/descartar", ("POST",)),  # BODY-DESPERTAR-REAL-7
        ("/experiments/{experiment_id}", ("GET",)),
        ("/experiments/{experiment_id}/close", ("POST",)),
        ("/experiments/{experiment_id}/day", ("POST",)),
        ("/garmin", ("GET",)),
        ("/garmin/codigo", ("POST",)),
        ("/garmin/conectar", ("POST",)),
        ("/garmin/desconectar", ("POST",)),
        ("/garmin/estado-vivo", ("GET",)),  # BODY-SYNC-VIVO-2 ruling 3
        ("/goals", ("GET",)),
        ("/goals/new", ("GET",)),
        ("/goals/new", ("POST",)),
        ("/goals/{goal_id}", ("GET",)),
        ("/goals/{goal_id}/activate", ("POST",)),
        ("/goals/{goal_id}/drop", ("POST",)),
        ("/goals/{goal_id}/enlaces", ("POST",)),
        ("/goals/{goal_id}/enlaces/{app_key}/quitar", ("POST",)),
        ("/goals/{goal_id}/finish", ("POST",)),
        ("/goals/{goal_id}/habits", ("POST",)),
        ("/goals/{goal_id}/pause", ("POST",)),
        ("/habits/{habit_id}/check", ("POST",)),
        ("/healthz", ("GET",)),
        ("/history", ("GET",)),
        ("/history/practica/{practica_id}", ("GET",)),  # BODY-HISTORIA-VIDA-2
        ("/history/practica/{practica_id}", ("POST",)),  # BODY-HISTORIA-VIDA-3 ruling 2
        ("/history/practica/{practica_id}/archive", ("POST",)),  # BODY-HISTORIA-VIDA-2
        ("/history/practicas", ("POST",)),  # BODY-HISTORIA-VIDA-2
        ("/how/{day}", ("GET",)),
        ("/login", ("GET",)),
        ("/login/cuenta/callback", ("GET",)),
        ("/login/cuenta/logout-token", ("POST",)),
        ("/login/cuenta/start", ("GET",)),
        ("/logout", ("POST",)),
        ("/mcp", ("POST",)),  # BODY-MCP-HTTP-1
        ("/periodo/{period_id}/sigue", ("POST",)),  # BODY-REVISION-DIA-PERIODO-1
        ("/privacidad", ("GET",)),
        ("/privacidad/borrar", ("POST",)),
        ("/privacidad/consentimiento", ("GET",)),
        ("/privacidad/consentimiento", ("POST",)),
        ("/privacidad/exportar", ("GET",)),
        ("/privacidad/fuente/borrar", ("POST",)),  # BODY-DESPLAZAMIENTO-3 ruling 4
        ("/settings", ("GET",)),
        ("/settings/aplicar_mcp", ("POST",)),  # BODY-MCP-APLICAR-1-B
        ("/settings/comidas_compartidas", ("POST",)),  # BODY-COMIDA-COMPARTIDA-1
        ("/settings/compartir", ("POST",)),
        ("/settings/language", ("POST",)),
        ("/settings/nutriente/proponer", ("POST",)),  # BODY-NUTRIENTE-OBJETIVO-1
        ("/settings/nutriente/propuestas/{propuesta_id}/aplicar", ("POST",)),  # BODY-NUTRIENTE-OBJETIVO-1
        ("/settings/nutriente/propuestas/{propuesta_id}/descartar", ("POST",)),  # BODY-NUTRIENTE-OBJETIVO-1
        ("/settings/nutriente/propuestas/{propuesta_id}/deshacer", ("POST",)),  # BODY-NUTRIENTE-OBJETIVO-1-M
        ("/settings/trabaja_turnos", ("POST",)),
        ("/shifts", ("GET",)),
        ("/shifts/como", ("GET",)),
        ("/shifts/confirm", ("POST",)),
        ("/shifts/confirm-all", ("POST",)),
        ("/shifts/dismiss", ("POST",)),
        ("/shifts/patron", ("GET",)),
        ("/shifts/patron", ("POST",)),
        ("/shifts/patron/quitar", ("POST",)),
        ("/shifts/undo", ("POST",)),  # BODY-TURNOS-DESHACER-LOTE-3-R
        ("/static", ()),
        ("/sw.js", ("GET",)),
        ("/trends", ("GET",)),
        ("/trends/racha-piso", ("POST",)),  # BODY-TENDENCIAS-RACHA-GRAFICO-1
    }

    def test_route_set_is_unchanged_by_the_ayuda_move(self):
        after = {(r.path, tuple(sorted(getattr(r, "methods", None) or [])))
                 for r in rutas_planas(web.app.routes)}
        self.assertEqual(len(after), len(self.BEFORE))
        self.assertEqual(after, self.BEFORE)


class MainEntryTest(unittest.TestCase):
    def test_main_reaches_uvicorn_run_with_no_streams(self):
        old_out, old_err, old_argv = sys.stdout, sys.stderr, sys.argv
        sys.stdout = None
        sys.stderr = None
        sys.argv = ["body.web"]
        try:
            with patch("uvicorn.run") as run:
                web.main()
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs.get("access_log"), False)
        finally:
            sys.stdout, sys.stderr, sys.argv = old_out, old_err, old_argv


if __name__ == "__main__":
    unittest.main()
