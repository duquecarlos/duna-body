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
