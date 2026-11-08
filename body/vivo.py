"""The launcher every scheduled task runs through (BODY-LIVE-CHECKOUT-1).

Carlos's web app and his scheduled Garmin pull / weekly file run from a SIBLING git worktree --
the "live tree" -- that only ever holds `main`, refreshed here at the start of every task. A
dispatched worker's unmerged branch lives in the repo's own working tree and is never served:
"restart the web task" means "serve main", even mid-slice, because the live tree is a different
checkout on disk from the one workers switch branches in.

`refrescar()` moves the live tree forward and REFUSES to touch anything that is not a linked
worktree -- in particular it never moves the repo's own main working tree, the one a dispatched
worker might have checked out to a branch at that moment. A refresh that cannot proceed (dirty,
not a worktree, git failure) is a fail-safe, not an abort: `main()` still runs the task against
whatever commit is already checked out there, which is always a `main` commit the tree was
detached at on some earlier, successful refresh -- never a branch.

Every task, including `web`, runs IN-PROCESS, via `runpy.run_module` right after the refresh --
never spawned and waited on, and never exec'd into a replacement process. Windows CPython has no
`exec`: `os.execve` there starts a NEW process and the launcher exits, so the PID Task Scheduler
launched would be gone -- "Body app web" would read as finished, its restart policy would never
watch the server, `Stop-ScheduledTask` would no longer stop it, and the next `Start-ScheduledTask`
would launch a second server on the same port. Running in-process instead keeps the same PID the
Task Scheduler action launched throughout the task's life, so `web`'s restart policy
(`-RestartCount`/`-RestartInterval` on "Body app web") watches the real server, and no second
process or console is ever created -- a `pythonw`-launched task stays consoleless. The task's own
`SystemExit` becomes this launcher's exit code; a normal return is 0.
"""
from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

from . import errores

TAREAS = {
    "web": "body.web",
    "pull": "body.pull",
    "shift_import": "body.shift_import",
    "vida_export": "body.vida_export",
    "subir_turnos": "body.subir_turnos",
    "traer_semana": "body.traer_semana",
}

_TIMEOUT_GIT = 10
_TIMEOUT_CHECKOUT = 30


def arbol_vivo() -> Path:
    """The directory this module's package lives in -- the tree running this process."""
    return Path(__file__).resolve().parents[1]


def _git(vivo: Path, *args: str, timeout: int = _TIMEOUT_GIT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(vivo), *args], capture_output=True, text=True, timeout=timeout
    )


def _rev_parse(vivo: Path, *args: str) -> str:
    resultado = _git(vivo, "rev-parse", *args)
    if resultado.returncode != 0:
        raise RuntimeError(resultado.stderr.strip() or "git rev-parse failed")
    return resultado.stdout.strip()


def _motivo(texto: str) -> str:
    primera = (texto or "error").strip().splitlines()
    primera = primera[0] if primera else "error"
    return primera[:40].replace(" ", "_") or "error"


def _ruta_absoluta(vivo: Path, texto: str) -> Path:
    ruta = Path(texto)
    return ruta if ruta.is_absolute() else (vivo / ruta).resolve()


def repo_principal(vivo: Path) -> Path:
    """The main repository that owns this worktree -- the parent of its common git dir."""
    common_dir = _rev_parse(vivo, "--git-common-dir")
    return _ruta_absoluta(vivo, common_dir).parent


def refrescar(vivo: Path) -> str:
    """Moves `vivo` to `main`'s current commit. Never touches the main working tree, never
    checks out a branch (always detached), and never acts on a tree that is not clean."""
    try:
        git_dir = _rev_parse(vivo, "--git-dir")
        common_dir = _rev_parse(vivo, "--git-common-dir")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return f"sin_cambio:{_motivo(errores.texto_seguro(exc))}"

    if _ruta_absoluta(vivo, git_dir) == _ruta_absoluta(vivo, common_dir):
        return "sin_cambio:no_es_arbol_vivo"

    estado = _git(vivo, "status", "--porcelain")
    if estado.returncode != 0:
        return f"sin_cambio:{_motivo(estado.stderr)}"
    if estado.stdout.strip():
        return "sin_cambio:sucio"

    try:
        antes = _rev_parse(vivo, "HEAD")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return f"sin_cambio:{_motivo(errores.texto_seguro(exc))}"

    try:
        checkout = _git(vivo, "checkout", "--detach", "main", timeout=_TIMEOUT_CHECKOUT)
    except subprocess.TimeoutExpired:
        return "sin_cambio:tiempo_agotado"
    if checkout.returncode != 0:
        return f"sin_cambio:{_motivo(checkout.stderr)}"

    try:
        despues = _rev_parse(vivo, "HEAD")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return f"sin_cambio:{_motivo(errores.texto_seguro(exc))}"

    return "actualizado" if antes != despues else "ya_al_dia"


def _modulos_body_inesperados() -> list[str]:
    """`body*` entries in `sys.modules` other than `body` and `body.vivo` -- modules a previous
    task in this same process could have left cached, surviving the refresh with stale code."""
    return sorted(
        nombre
        for nombre in sys.modules
        if (nombre == "body" or nombre.startswith("body.")) and nombre not in ("body", "body.vivo")
    )


def _uso() -> None:
    print("uso: python -m body.vivo <tarea> [args...]", file=sys.stderr)
    print("tareas: " + ", ".join(sorted(TAREAS)), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in TAREAS:
        _uso()
        return 2
    tarea, *resto = argv

    vivo = arbol_vivo()
    if _modulos_body_inesperados():
        resultado = "sin_cambio:modulos_previos"
    else:
        resultado = refrescar(vivo)
    try:
        sha = _rev_parse(vivo, "--short", "HEAD")
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        sha = "?"
    if sys.stdout is not None:
        print(f"{resultado} {vivo.name} {sha}")

    principal = repo_principal(vivo)
    os.environ.setdefault("BODY_ENV_FILE", str(principal / ".env"))

    os.chdir(vivo)
    vivo_str = str(vivo)
    if vivo_str in sys.path:
        sys.path.remove(vivo_str)
    sys.path.insert(0, vivo_str)

    sys.argv = [TAREAS[tarea], *resto]
    try:
        runpy.run_module(TAREAS[tarea], run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        if sys.stdout is not None:
            print(exc.code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
