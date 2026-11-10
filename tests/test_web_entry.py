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
        ("/api/turnos", ("POST",)),
        ("/api/vida/semana", ("GET",)),
        ("/asistente/conversacion/{conversacion_id}", ("GET",)),
        ("/asistente/preguntar", ("POST",)),
        ("/asistente/propuestas/{propuesta_id}/aplicar", ("POST",)),
        ("/asistente/propuestas/{propuesta_id}/descartar", ("POST",)),
        ("/asistente/propuestas/{propuesta_id}/deshacer", ("POST",)),
        ("/ayuda", ("GET",)),
        ("/day/{day}", ("GET",)),
        ("/day/{day}/noche-inusual/visto", ("POST",)),  # BODY-NOCHE-INUSUAL-1
        ("/experiments/{experiment_id}", ("GET",)),
        ("/experiments/{experiment_id}/close", ("POST",)),
        ("/experiments/{experiment_id}/day", ("POST",)),
        ("/garmin", ("GET",)),
        ("/garmin/codigo", ("POST",)),
        ("/garmin/conectar", ("POST",)),
        ("/garmin/desconectar", ("POST",)),
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
        ("/how/{day}", ("GET",)),
        ("/login", ("GET",)),
        ("/login/cuenta/callback", ("GET",)),
        ("/login/cuenta/logout-token", ("POST",)),
        ("/login/cuenta/start", ("GET",)),
        ("/logout", ("POST",)),
        ("/privacidad", ("GET",)),
        ("/privacidad/borrar", ("POST",)),
        ("/privacidad/consentimiento", ("GET",)),
        ("/privacidad/consentimiento", ("POST",)),
        ("/privacidad/exportar", ("GET",)),
        ("/settings", ("GET",)),
        ("/settings/compartir", ("POST",)),
        ("/settings/language", ("POST",)),
        ("/settings/trabaja_turnos", ("POST",)),
        ("/shifts", ("GET",)),
        ("/shifts/como", ("GET",)),
        ("/shifts/confirm", ("POST",)),
        ("/shifts/confirm-all", ("POST",)),
        ("/shifts/dismiss", ("POST",)),
        ("/shifts/patron", ("GET",)),
        ("/shifts/patron", ("POST",)),
        ("/shifts/patron/quitar", ("POST",)),
        ("/static", ()),
        ("/sw.js", ("GET",)),
        ("/trends", ("GET",)),
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
