"""Local-today math, in one place (BODY-FECHA-FUTURA-1).

`body/rutas/comun.py`'s `_today_local` and `body/agente/herramientas.py`'s `_hoy_local` had each grown
the same timezone-to-today logic on their own; both now delegate here. This is also the one place the
hub's cross-product rule (hit_01m6w6cn8x) lives: an ACTUAL record -- something that already
happened -- refuses a date past the person's local today plus one day of tolerance (a phone clock a
little ahead near midnight must never have today's pre-filled date refused). PLANS (a goal deadline,
a shift-pattern start day, an imported shift) are untouched by this module.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TOLERANCIA_DIAS = 1


def hoy_local(person: dict) -> date:
    tz = ZoneInfo(person.get("timezone") or "America/Bogota")
    return datetime.now(tz).date()


def fecha_real_valida(day: date, hoy: date) -> bool:
    return day <= hoy + timedelta(days=TOLERANCIA_DIAS)


def lunes_de(d: date) -> date:
    """Monday of d's ISO week (weeks start on Monday). No timezone: the caller passes a date
    already local to whoever's week this is."""
    return d - timedelta(days=d.weekday())


def rachas_de_estados(estados: list[str], *, on: str = "on", bridge: str = "bridge",
                       bridge_max: int = 0) -> list[dict]:
    """BODY-HISTORIA-VIDA-1 ruling 4: the run-detection `entreno_racha.racha_semanal` has always
    used (consecutive weeks meeting its own floor), generalised so body/practicas.py's day-level
    streaks and season detection can share it instead of a second copy.

    Consecutive runs of `on` entries, allowing up to `bridge_max` consecutive `bridge` entries
    INSIDE a run without breaking it (anything else -- a longer bridge stretch or any other value --
    ends it). Each run is {"inicio": <index of its first `on`>, "fin": <index of its last CONFIRMED
    `on`>, "puentes": [<bridged indices actually absorbed>], "abierta": <bool>}. A trailing bridge
    stretch at the end of `estados`, not yet followed by another `on`, is left out of `puentes` and
    never extends `fin` -- the same "neither break nor extend the run" rule BODY-HISTORIA-VIDA-1's
    spec gives practicas.py's own day streaks. `abierta` is True for at most one run, the last one,
    and only when `estados` ends before that run was ever broken (still open, possibly mid-bridge);
    `entreno_racha.racha_semanal` (bridge_max=0) never produces a bridge, so its own runs are
    unaffected and it ignores `abierta`/`puentes`, reading only `inicio`/`fin`."""
    runs: list[dict] = []
    inicio = fin = None
    puentes_confirmados: list[int] = []
    pendientes: list[int] = []
    for i, e in enumerate(estados):
        if e == on:
            if inicio is None:
                inicio = i
            else:
                puentes_confirmados.extend(pendientes)
            fin = i
            pendientes = []
        elif e == bridge and inicio is not None and len(pendientes) < bridge_max:
            pendientes.append(i)
        else:
            if inicio is not None:
                runs.append({"inicio": inicio, "fin": fin, "puentes": puentes_confirmados, "abierta": False})
            inicio = fin = None
            puentes_confirmados = []
            pendientes = []
    if inicio is not None:
        runs.append({"inicio": inicio, "fin": fin, "puentes": puentes_confirmados, "abierta": True})
    return runs
