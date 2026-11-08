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

`web` replaces this process (`os.execve`) rather than being spawned and waited on: on Windows
that keeps the same PID the Task Scheduler action launched, so the task's own restart policy
(`-RestartCount`/`-RestartInterval` on "Body app web") watches the real server process, not a
thin wrapper around it, and no second process or console is created -- a `pythonw`-launched task
stays consoleless because there never is a second process. Every other task is one-shot: it is
simplest, and correct, to run it and exit with its own code.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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
        return f"sin_cambio:{_motivo(str(exc))}"

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
        return f"sin_cambio:{_motivo(str(exc))}"

    try:
        checkout = _git(vivo, "checkout", "--detach", "main", timeout=_TIMEOUT_CHECKOUT)
    except subprocess.TimeoutExpired:
        return "sin_cambio:tiempo_agotado"
    if checkout.returncode != 0:
        return f"sin_cambio:{_motivo(checkout.stderr)}"

    try:
        despues = _rev_parse(vivo, "HEAD")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return f"sin_cambio:{_motivo(str(exc))}"

    return "actualizado" if antes != despues else "ya_al_dia"


def _despachar(tarea: str, cmd: list[str], cwd: Path, env: dict) -> int:
    if tarea == "web":
        os.chdir(cwd)
        os.execve(cmd[0], cmd, env)  # pragma: no cover -- replaces this process, never returns
    return subprocess.run(cmd, cwd=cwd, env=env).returncode


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
    resultado = refrescar(vivo)
    try:
        sha = _rev_parse(vivo, "--short", "HEAD")
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        sha = "?"
    print(f"{resultado} {vivo.name} {sha}")

    principal = repo_principal(vivo)
    env = dict(os.environ)
    env.setdefault("BODY_ENV_FILE", str(principal / ".env"))

    cmd = [sys.executable, "-m", TAREAS[tarea], *resto]
    return _despachar(tarea, cmd, vivo, env)


if __name__ == "__main__":
    sys.exit(main())
