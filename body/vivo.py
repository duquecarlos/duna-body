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

The one exception to the PID rule (BODY-VIVO-TAREA-NUEVA-1): a task name this running module's
`TAREAS` does not recognize, but the refresh it just ran moved the tree past, goes through a CHILD
process instead -- see `_relanzar_en_arbol_nuevo`. The PID rule protects a task Task Scheduler
launches by a name the running code already knows; a name it does not know can only have been
added by code newer than this process holds, and no amount of staying in-process makes this
process able to run code it never imported.
"""
from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, errores

TAREAS = {
    "web": "body.web",
    "pull": "body.pull",
    "shift_import": "body.shift_import",
    "vida_export": "body.vida_export",
    "subir_turnos": "body.subir_turnos",
    "traer_semana": "body.traer_semana",
    "vida_dia": "body.vida_dia",
    # The scheduled morning run (BODY-VIDA-DIA-1): refresh today's pull first, below, then this
    # same entry writes the day's vida-v1 file -- from `resto` runs through here unchanged.
    "manana": "body.vida_dia",
    # BODY-MOMENTOS-PULL-1: runs through body.pull's own --intradia flag -- see PREFIJO_ARGV below,
    # which is how `python -m body.vivo intradia --dias N` reaches it without a --intradia the
    # person never typed.
    "intradia": "body.pull",
    # BODY-DESPERTAR-1: the wake check -- every ~20 minutes, 04:00-13:00, writes
    # BODY_HOME/vida/despertar-<day>.json as soon as last night's sleep end is known.
    "despertar": "body.despertar",
    # BODY-GARMIN-IMPORT-1: `python -m body.vivo importar_garmin <path-to-export.zip> [...]` --
    # arguments passed straight through to body.importar_garmin.
    "importar_garmin": "body.importar_garmin",
}

# Argv prepended before a task's own arguments, for a task that shares a module with another task
# and needs its own flag to select the right codepath -- today only `intradia`.
PREFIJO_ARGV: dict[str, list[str]] = {
    "intradia": ["--intradia"],
}

# A module run BEFORE `TAREAS[tarea]`, with its own fixed argv, for tasks that need one. Only
# `manana` has one today: pull today's figures before vida_dia writes them out. Any failure here
# (no network, Garmin down, a raised exception or a non-zero exit) is swallowed -- vida_dia still
# runs and writes the file from whatever is already stored, with its own `missing` list telling
# the truth, instead of the whole scheduled run failing silently with no file written at all.
PASOS_PREVIOS: dict[str, tuple[str, list[str]]] = {
    "manana": ("body.pull", ["--days", "1"]),
}

_TIMEOUT_GIT = 10
_TIMEOUT_CHECKOUT = 30

ESTADO_NOMBRE = "vivo-estado.json"


def _modulos_body_actuales() -> set[str]:
    """Every `body*` entry in `sys.modules` right now."""
    return {nombre for nombre in sys.modules if nombre == "body" or nombre.startswith("body.")}


# Captured once this module has finished importing (vivo itself, plus whatever it pulled in
# above -- today `body.config` and `body.errores`). Anything beyond this baseline, found later
# in the same process, is a module a PREVIOUS task left cached (BODY-VIVO-REFRESCO-1).
_MODULOS_AL_CARGAR = frozenset(_modulos_body_actuales())


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
    """`body*` entries in `sys.modules` beyond the baseline this module recorded for itself when
    IT finished importing -- modules a previous task in this same process could have left cached,
    surviving the refresh with stale code."""
    return sorted(_modulos_body_actuales() - _MODULOS_AL_CARGAR)


def _escribir_estado(vivo: Path, resultado: str, tarea: str, sha: str) -> None:
    """A small JSON status file in BODY_HOME recording this run's outcome -- never a figure. A
    failure to write it must not stop the task (fail-safe, like `refrescar` itself)."""
    try:
        sha_main = _rev_parse(vivo, "--short", "main")
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        sha_main = "?"
    estado = {
        "resultado": resultado,
        "tarea": tarea,
        "sha": sha,
        "sha_main": sha_main,
        "marca_tiempo_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        config.ensure_home()
        (config.BODY_HOME / ESTADO_NOMBRE).write_text(
            json.dumps(estado, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        pass


def _uso() -> None:
    print("uso: python -m body.vivo <tarea> [args...]", file=sys.stderr)
    print("tareas: " + ", ".join(sorted(TAREAS)), file=sys.stderr)


def _tareas_conocidas_por_el_arbol(vivo: Path) -> set[str]:
    """Which task names the REFRESHED tree's own `body.vivo` knows, read by running its
    `--tareas` in a child process -- never by importing it into this one. This process already
    holds the OLD module (that is the whole defect BODY-VIVO-TAREA-NUEVA-1 fixes: `TAREAS` here
    can be stale the moment a refresh moves the tree), and `_modulos_body_inesperados` exists
    precisely to catch a previous task's stale import surviving in this same process -- importing
    the new code here on purpose would risk the identical mismatch."""
    try:
        resultado = subprocess.run(
            [sys.executable, "-m", "body.vivo", "--tareas"],
            cwd=str(vivo), capture_output=True, text=True, timeout=_TIMEOUT_CHECKOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if resultado.returncode != 0:
        return set()
    return {nombre.strip() for nombre in resultado.stdout.strip().split(",") if nombre.strip()}


def _relanzar_en_arbol_nuevo(vivo: Path, argv: list[str]) -> int:
    """The ONE case that spawns a child process instead of running in-process: a task name this
    running module's `TAREAS` does not recognize, but the tree the refresh just moved to does. The
    PID rule in this module's docstring is about tasks Task Scheduler launches by a name the
    running code already knows -- it has no bearing on a name that can only come from code newer
    than this process, which this process cannot run no matter how it is called. The child is the
    new launcher, given the exact same argv; it refreshes again, finds the tree already current,
    and runs the task in-process itself."""
    resultado = subprocess.run([sys.executable, "-m", "body.vivo", *argv], cwd=str(vivo))
    return resultado.returncode


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        _uso()
        return 2
    if argv == ["--tareas"]:
        print(",".join(sorted(TAREAS)))
        return 0
    tarea, *resto = argv
    conocida = tarea in TAREAS

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
    _escribir_estado(vivo, resultado, tarea, sha)

    if not conocida:
        if resultado == "actualizado" and tarea in _tareas_conocidas_por_el_arbol(vivo):
            return _relanzar_en_arbol_nuevo(vivo, argv)
        _uso()
        return 2

    principal = repo_principal(vivo)
    os.environ.setdefault("BODY_ENV_FILE", str(principal / ".env"))

    os.chdir(vivo)
    vivo_str = str(vivo)
    if vivo_str in sys.path:
        sys.path.remove(vivo_str)
    sys.path.insert(0, vivo_str)

    paso_previo = PASOS_PREVIOS.get(tarea)
    if paso_previo is not None:
        modulo_previo, argv_previo = paso_previo
        sys.argv = [modulo_previo, *argv_previo]
        try:
            runpy.run_module(modulo_previo, run_name="__main__", alter_sys=True)
        except SystemExit:
            pass
        except Exception as exc:
            if sys.stdout is not None:
                print(f"{modulo_previo} failed before {tarea}: {errores.texto_seguro(exc)}")

    sys.argv = [TAREAS[tarea], *PREFIJO_ARGV.get(tarea, []), *resto]
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
