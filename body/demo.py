"""Seeds a scratch BODY_HOME with an invented person for tutorial recordings (tutoriales-v1 section 8,
step 2: "Demo tenant and catalog"). Growth drives this tenant with Playwright; nothing here is a real
person's data -- every figure is generated from a fixed random seed, in a plausible range, never read
from a real database.

    python -m body.demo --home <folder> [--dias 90] [--idioma es|en]

REFUSES (non-zero exit, a plain message, nothing written) when `--home` is missing, or resolves to the
default BODY_HOME (`~/.body-app`), the current `BODY_HOME` environment value, or `/srv/duna/data/body`,
or when the folder already holds a `body.db` that this script did not create (no `DEMO` marker file next
to it). Re-running into the same demo folder (the marker is there) rebuilds it from scratch, so two runs
give identical data (same seed, fresh tables) -- only the marker's and the rows' own creation timestamps
differ.

Prints only counts and the folder; never a health figure.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import fechas

SEED = 20261105
DIAS_DEFAULT = 90
MARKER_NAME = "DEMO"
_BANNED_HOME = Path("/srv/duna/data/body")

_CYCLE = ["morning", "afternoon", "night", "day"]
_CYCLE_ANCHOR_MONDAY = date(2024, 1, 1)  # a Monday, fixed, so the shift pattern never depends on today
_SHIFT_HOURS = {  # kind -> (start "HH:MM", end "HH:MM", end falls on the next day)
    "morning": ("06:00", "14:00", False),
    "afternoon": ("14:00", "22:00", False),
    "night": ("22:00", "06:00", True),
    "day": ("08:00", "16:00", False),
}
_SHIFT_CODE = {"morning": "M", "afternoon": "T", "night": "N", "day": "D"}

CORE_DENSE = ["resting_hr", "hrv_last_night", "sleep_minutes", "body_battery_wake", "stress_avg",
              "body_battery_high", "body_battery_low", "steps"]
SECONDARY = ["hrv_weekly_avg", "sleep_score", "active_kcal", "intensity_minutes", "sleep_need_min",
             "sleep_deep_min", "sleep_light_min", "sleep_rem_min", "sleep_awake_min",
             "body_battery_sleep_charge", "respiration_awake", "high_stress_min"]
CARDIO_SPARSE = ["vo2max", "vo2max_cycling", "fitness_age"]

# Self-measured (BODY-ENTRENO-SCHEMA-1): the person's own figures, never the watch's -- sparser
# than daily and stored with source='self' (_seed_self_metrics), never mixed into save_day's
# per-day 'garmin' batch.
SELF_MEASURED = ["flex_alcance_cm", "snoring_min"]

RANGES = {
    "resting_hr": (48, 62), "hrv_last_night": (40, 90), "hrv_weekly_avg": (45, 85),
    "sleep_minutes": (320, 480), "sleep_score": (40, 95), "stress_avg": (15, 45),
    "body_battery_high": (70, 100), "body_battery_low": (15, 40), "body_battery_wake": (55, 95),
    "steps": (2000, 12000), "active_kcal": (150, 700), "intensity_minutes": (0, 60),
    "sleep_need_min": (420, 480), "sleep_deep_min": (40, 110), "sleep_light_min": (120, 220),
    "sleep_rem_min": (40, 100), "sleep_awake_min": (5, 40), "body_battery_sleep_charge": (10, 60),
    "respiration_awake": (12, 18), "high_stress_min": (0, 90),
    "vo2max": (38, 46), "vo2max_cycling": (40, 48), "fitness_age": (28, 42),
    "flex_alcance_cm": (0, 25), "snoring_min": (0, 70),
}
# A night shift (or the recovery day right after) costs sleep and raises stress -- the correlation
# the "turnos" chapter's tutorial task is built to show. Added to the sampled value, then clamped.
NIGHT_EFFECT = {"resting_hr": 6, "hrv_last_night": -12, "sleep_minutes": -70, "stress_avg": 10,
                "body_battery_wake": -15, "body_battery_low": -10, "body_battery_high": -8}
_PCT_KEYS = {"body_battery_high", "body_battery_low", "body_battery_wake", "sleep_score"}

ACTIVITY_TYPES = [
    {"type": "running", "name": "Carrera", "dur": (20, 55), "dist": (3, 10), "hr": (130, 165), "kcal": (250, 550)},
    {"type": "cycling", "name": "Ciclismo", "dur": (30, 90), "dist": (10, 35), "hr": (110, 150), "kcal": (300, 700)},
    {"type": "strength_training", "name": "Fuerza", "dur": (30, 60), "dist": None, "hr": (100, 135),
     "kcal": (180, 320)},
    {"type": "walking", "name": "Caminata", "dur": (20, 45), "dist": (1.5, 4), "hr": (90, 115), "kcal": (120, 220)},
]

_GOAL_TITLES = {
    "es": {"sleep": "Dormir más", "resting_hr": "Bajar la frecuencia cardíaca en reposo", "steps": "Caminar más"},
    "en": {"sleep": "Sleep more", "resting_hr": "Lower my resting heart rate", "steps": "Walk more"},
}
_HABIT_TITLE = {"es": "Dormir antes de un turno nocturno", "en": "Sleep before a night shift"}

# BODY-HOGAR-MIEMBRO-1: a second, invented demo person ("Ana") in one household with the demo
# person, and a shared goal SHE creates, so the demo person -- browsing as themselves -- sees the
# member view (BODY-HOGAR-PLAN-1 rulings 1-2).
_ANA_NAME = "Ana"
_HOGAR_ID = "demo-hogar-1"
_HOGAR_GOAL_TITLE = {"es": "Preparar el ascenso", "en": "Prepare for the climb"}
_HOGAR_HABIT_BOTH = {"es": "Entrenar piernas", "en": "Train legs"}
_HOGAR_HABIT_ANA = {"es": "Revisar el equipo", "en": "Check the gear"}


@dataclass
class _DayData:
    """Same shape body/db.py's save_day() reads from a real adapter's DayData -- kept local to this
    module so the demo never imports body/garmin_adapter.py (Tier 1)."""
    day: str
    source: str = "garmin"  # db.days_with_data() only counts this source; see module docstring
    raw: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    sleep_start: str | None = None
    sleep_end: str | None = None
    activities: list = field(default_factory=list)


def _refuse(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def _forbidden_home(target: Path) -> str | None:
    resolved = target.expanduser().resolve()
    default_home = (Path.home() / ".body-app").resolve()
    if resolved == default_home:
        return f"refusing: {target} resolves to the default BODY_HOME ({default_home}) -- pass a scratch folder"
    env_value = os.environ.get("BODY_HOME")
    if env_value:
        env_home = Path(env_value).expanduser().resolve()
        if resolved == env_home:
            return f"refusing: {target} resolves to the current BODY_HOME env value ({env_home}) -- pass a scratch folder"
    banned = _BANNED_HOME.resolve()
    if resolved == banned:
        return f"refusing: {target} is the production data folder -- pass a scratch folder"
    return None


def _week_index(d: date) -> int:
    monday = fechas.lunes_de(d)
    return (monday - _CYCLE_ANCHOR_MONDAY).days // 7


def _shift_kind_for(d: date) -> str:
    if d.weekday() >= 5:
        return "off"
    return _CYCLE[_week_index(d) % 4]


_RECOVERY_FACTOR = {0: 1.0, 1: 0.7, 2: 0.4, 3: 0.15}  # days since the block's last night -> effect scale


def _recovery_factor(d: date) -> float:
    """0 while in a night block; tapers off over a few days after it (BODY-TURNOS-COSTO-1: the demo
    must show a believable multi-day recovery, not an instant one)."""
    if _shift_kind_for(d) == "night":
        return 1.0
    for back, factor in _RECOVERY_FACTOR.items():
        if back and _shift_kind_for(d - timedelta(back)) == "night":
            return factor
    return 0.0


def _value(rng: random.Random, key: str, factor: float) -> float:
    lo, hi = RANGES[key]
    v = rng.uniform(lo, hi)
    if factor:
        v += NIGHT_EFFECT.get(key, 0) * factor
    v = max(0.0, v)
    if key in _PCT_KEYS:
        v = min(100.0, v)
    return round(v, 1)


def _import_shifts(conn, shift_import, person_id: int, days: list[date], file_name: str, received: str) -> int | None:
    if not days:
        return None
    shifts = []
    for d in days:
        kind = _shift_kind_for(d)
        if kind == "off":
            shifts.append({"day": d.isoformat(), "kind": "off"})
            continue
        start_h, end_h, crosses = _SHIFT_HOURS[kind]
        end_day = d + timedelta(1) if crosses else d
        shifts.append({
            "day": d.isoformat(), "kind": kind, "code": _SHIFT_CODE[kind],
            "start_local": f"{d.isoformat()}T{start_h}:00", "end_local": f"{end_day.isoformat()}T{end_h}:00",
        })
    extracted_at = datetime.combine(days[-1], datetime.min.time()).isoformat(timespec="seconds")
    data = {"source": {"received": received, "extracted_at": extracted_at}, "person": "Demo",
            "timezone": "America/Bogota", "shifts": shifts}
    return shift_import.propose_data(conn, data, file_name, person_id)


def _avg_metric(conn, db, key: str, days: list[date], person_id: int) -> float | None:
    vals = list(db.series(conn, key, days[0].isoformat(), days[-1].isoformat(), person_id).values())
    return round(statistics.fmean(vals), 1) if vals else None


def _seed_person(conn, db, idioma: str) -> int:
    person_id = db.local_person_id(conn)
    conn.execute("UPDATE person SET display_name = 'Demo', timezone = 'America/Bogota' WHERE id = ?", (person_id,))
    db.set_language(conn, idioma, person_id)
    return person_id


def _day_metrics(rng: random.Random, d: date, factor: float) -> tuple[dict, str | None, str | None]:
    """One day's readings (CORE_DENSE, SECONDARY, the twice-monthly CARDIO_SPARSE batch) and, when
    sleep_minutes landed, that night's sleep window -- in the same draw order _seed_days always
    used, since the two-run determinism test depends on the order, not just the final values."""
    metrics = {}
    for key in CORE_DENSE:
        if rng.random() < 0.92:
            metrics[key] = _value(rng, key, factor)
    for key in SECONDARY:
        if rng.random() < 0.75:
            metrics[key] = _value(rng, key, factor)
    if d.day % 14 == 0:
        for key in CARDIO_SPARSE:
            metrics[key] = _value(rng, key, 0.0)

    sleep_start = sleep_end = None
    if "sleep_minutes" in metrics:
        minutes = metrics["sleep_minutes"]
        if _shift_kind_for(d - timedelta(1)) == "night":
            start_dt = datetime.combine(d, datetime.min.time()) + timedelta(hours=8, minutes=rng.randint(0, 59))
        else:
            start_dt = (datetime.combine(d - timedelta(1), datetime.min.time())
                        + timedelta(hours=22, minutes=rng.randint(0, 59)))
        end_dt = start_dt + timedelta(minutes=minutes)
        sleep_start, sleep_end = start_dt.isoformat(), end_dt.isoformat()

    return metrics, sleep_start, sleep_end


def _day_activity(rng: random.Random, d: date) -> dict | None:
    """One invented activity on D, 30% of the time, never on a night-shift day."""
    if _shift_kind_for(d) == "night":
        return None
    if rng.random() >= 0.3:
        return None
    act = rng.choice(ACTIVITY_TYPES)
    dist = rng.uniform(*act["dist"]) * 1000 if act["dist"] else None
    return {
        "source_id": f"demo-{d.isoformat()}-1", "start_local": f"{d.isoformat()}T18:{rng.randint(0, 59):02d}:00",
        "type": act["type"], "name": act["name"], "duration_s": round(rng.uniform(*act["dur"]) * 60),
        "distance_m": round(dist) if dist else None, "avg_hr": round(rng.uniform(*act["hr"])),
        "max_hr": round(rng.uniform(*act["hr"]) + 15), "calories": round(rng.uniform(*act["kcal"])),
    }


def _seed_days(conn, db, rng: random.Random, all_days: list[date], person_id: int) -> tuple[int, int]:
    """Every day's save_day call, in order -- metrics then that day's activity, matching the draw
    order a single rng.Random(SEED) has always produced them in."""
    metric_rows = activity_rows = 0
    for d in all_days:
        factor = _recovery_factor(d)
        metrics, sleep_start, sleep_end = _day_metrics(rng, d, factor)
        metric_rows += len(metrics)
        activity = _day_activity(rng, d)
        if activity:
            activity_rows += 1
        db.save_day(conn, _DayData(day=d.isoformat(), metrics=metrics, sleep_start=sleep_start, sleep_end=sleep_end,
                                    activities=[activity] if activity else []), person_id)
    conn.commit()
    return metric_rows, activity_rows


def _seed_self_metrics(conn, db, rng: random.Random, all_days: list[date], person_id: int) -> int:
    """The two generic self-measured metric_def rows (BODY-ENTRENO-SCHEMA-1), sparser than the
    watch's daily figures: a flexibility reach about once a week, a snoring minutes-per-night
    value most nights -- both with source='self', via db.set_metric so they never ride along in
    save_day's single 'garmin'-sourced batch."""
    rows = 0
    for d in all_days:
        if d.weekday() == 0:
            db.set_metric(conn, d.isoformat(), "flex_alcance_cm", _value(rng, "flex_alcance_cm", 0.0),
                           "self", person_id)
            rows += 1
        if rng.random() < 0.85:
            db.set_metric(conn, d.isoformat(), "snoring_min", _value(rng, "snoring_min", 0.0), "self", person_id)
            rows += 1
    conn.commit()
    return rows


def _seed_shifts(conn, shift_import, turnos_costo, person_id: int,
                  all_days: list[date]) -> tuple[int | None, int | None, int, int]:
    confirmed_days, proposed_days = all_days[:-7], all_days[-7:]
    confirmed_id = _import_shifts(conn, shift_import, person_id, confirmed_days, "turnos-demo-confirmados.json",
                                   received=confirmed_days[0].isoformat())
    if confirmed_id is not None:
        shift_import.confirm(conn, confirmed_id)
    proposed_id = _import_shifts(conn, shift_import, person_id, proposed_days, "turnos-demo-propuestos.json",
                                  received=proposed_days[0].isoformat())
    night_weeks = len({_week_index(d) for d in all_days if _shift_kind_for(d) == "night"})
    night_blocks = len(turnos_costo.bloques_noche(conn, person_id, all_days[0].isoformat(), all_days[-1].isoformat()))
    return confirmed_id, proposed_id, night_weeks, night_blocks


def _seed_goals_and_habits(conn, db, goals, rng: random.Random, all_days: list[date], anchor: date,
                            first_monday: date, anchor_monday: date, person_id: int,
                            idioma: str) -> tuple[int, int]:
    """The demo person's three metric goals, one habit, their weekly checks and the habit's
    checks -- returns (goal weekly checks, habit checks)."""
    window = all_days[:14]
    base_sleep = _avg_metric(conn, db, "sleep_minutes", window, person_id) or 420.0
    base_rhr = _avg_metric(conn, db, "resting_hr", window, person_id) or 58.0
    base_steps = _avg_metric(conn, db, "steps", window, person_id) or 6000.0
    start_day = all_days[0].isoformat()
    deadline = (anchor + timedelta(days=56)).isoformat()
    titles = _GOAL_TITLES[idioma]

    gid_sleep = goals.propose(conn, person_id, titles["sleep"], start_day, kind="metric", metric_key="sleep_minutes",
                               direction="raise", target_value=round(base_sleep + 40, 1), baseline_value=base_sleep,
                               deadline=deadline)
    gid_rhr = goals.propose(conn, person_id, titles["resting_hr"], start_day, kind="metric", metric_key="resting_hr",
                             direction="lower", target_value=round(base_rhr - 5, 1), baseline_value=base_rhr,
                             deadline=deadline)
    gid_steps = goals.propose(conn, person_id, titles["steps"], start_day, kind="metric", metric_key="steps",
                               direction="raise", target_value=round(base_steps + 2000, 1), baseline_value=base_steps,
                               deadline=deadline)
    for gid in (gid_sleep, gid_rhr, gid_steps):
        goals.activate(conn, gid)

    habit_id = goals.add_habit(conn, gid_sleep, _HABIT_TITLE[idioma],
                                {"per": "week", "times": 3, "context": "before_night"})

    checks = 0
    monday = first_monday
    while monday <= anchor_monday:
        for gid in (gid_sleep, gid_rhr, gid_steps):
            if goals.weekly_check(conn, gid, person_id, monday.isoformat()):
                checks += 1
        monday += timedelta(7)

    habit_checks = 0
    recent_monday = max(first_monday, anchor_monday - timedelta(35))
    monday = recent_monday
    while monday <= anchor_monday:
        for dd in goals._context_due_days(conn, person_id, monday, "before_night"):
            if date.fromisoformat(dd) <= anchor:
                goals.check_habit(conn, habit_id, person_id, dd, rng.random() < 0.7, hoy=anchor)
                habit_checks += 1
        monday += timedelta(7)

    return checks, habit_checks


def _seed_household(conn, db, goals, enlaces, compartir, rng: random.Random, all_days: list[date], anchor: date,
                     first_monday: date, anchor_monday: date, person_id: int,
                     idioma: str) -> tuple[int, int, int]:
    """A second demo person (Ana), one household with the demo person, her shared figure, and a
    household goal she creates -- returns (her seeded metric rows, goal weekly checks, habit checks)."""
    start_day = all_days[0].isoformat()
    ana_cur = conn.execute(
        "INSERT INTO person (display_name, timezone, language) VALUES (?, 'America/Bogota', ?)",
        (_ANA_NAME, idioma),
    )
    ana_id = ana_cur.lastrowid
    hogar_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO hogar_miembro (person_id, hogar_id, actualizado_at) VALUES (?, ?, ?)",
        [(person_id, _HOGAR_ID, hogar_now), (ana_id, _HOGAR_ID, hogar_now)])
    conn.commit()
    # Ana shares one figure with the household (BODY-HOGAR-COMPARTIR-1), so the demo person's own
    # Hoy shows the «Tu hogar» card. She needs her OWN short invented history of it -- never the
    # demo person's numbers copied -- or the card can only ever say "sin dato" and the value path
    # is unmeasurable; a few of her own days, inside the same catalogue range, are enough.
    ana_days = all_days[-5:]
    for d in ana_days:
        db.set_metric(conn, d.isoformat(), "resting_hr", _value(rng, "resting_hr", 0.0), "garmin", ana_id)
    conn.commit()
    compartir.compartir(conn, ana_id, "resting_hr")

    hogar_deadline = (anchor + timedelta(days=84)).isoformat()
    gid_hogar = goals.propose(conn, ana_id, _HOGAR_GOAL_TITLE[idioma], start_day, area="adventure",
                              kind="milestone", deadline=hogar_deadline, hogar_id=_HOGAR_ID,
                              visibility="household")
    goals.activate(conn, gid_hogar)
    # DESIGN rule 17 (BODY-HOGAR-ENLACE-1): the adventure goal links to its trip in Lugares -- an
    # invented demo id, never a real one.
    enlaces.enlazar(conn, gid_hogar, "lugares", "4821")
    habit_both_id = goals.add_habit(conn, gid_hogar, _HOGAR_HABIT_BOTH[idioma], {"per": "week", "times": 2})
    goals.add_habit(conn, gid_hogar, _HOGAR_HABIT_ANA[idioma], {"per": "week", "times": 1},
                     assignee_person_id=ana_id)

    hogar_checks = hogar_habit_checks = 0
    hogar_recent_monday = max(first_monday, anchor_monday - timedelta(21))
    monday = hogar_recent_monday
    while monday <= anchor_monday:
        for dd in (monday.isoformat(), (monday + timedelta(3)).isoformat()):
            if date.fromisoformat(dd) <= anchor:
                goals.check_habit(conn, habit_both_id, ana_id, dd, rng.random() < 0.7, hoy=anchor)
                hogar_habit_checks += 1
        if goals.weekly_check(conn, gid_hogar, ana_id, monday.isoformat()):
            hogar_checks += 1
        monday += timedelta(7)

    return len(ana_days), hogar_checks, hogar_habit_checks


def _seed(conn, *, dias: int, idioma: str) -> dict[str, int]:
    from . import compartir, db, enlaces, goals, shift_import, turnos_costo

    person_id = _seed_person(conn, db, idioma)

    rng = random.Random(SEED)
    anchor = date.today()
    all_days = [anchor - timedelta(dias - 1 - i) for i in range(dias)]
    first_monday, anchor_monday = fechas.lunes_de(all_days[0]), fechas.lunes_de(anchor)

    metric_rows, activity_rows = _seed_days(conn, db, rng, all_days, person_id)
    metric_rows += _seed_self_metrics(conn, db, rng, all_days, person_id)

    confirmed_id, proposed_id, night_weeks, night_blocks = _seed_shifts(
        conn, shift_import, turnos_costo, person_id, all_days)

    checks, habit_checks = _seed_goals_and_habits(
        conn, db, goals, rng, all_days, anchor, first_monday, anchor_monday, person_id, idioma)

    ana_metric_rows, hogar_checks, hogar_habit_checks = _seed_household(
        conn, db, goals, enlaces, compartir, rng, all_days, anchor, first_monday, anchor_monday, person_id, idioma)
    metric_rows += ana_metric_rows

    return {
        "personas": 2, "dias_sembrados": dias, "valores_diarios": metric_rows, "actividades": activity_rows,
        "lotes_turnos_confirmados": 1 if confirmed_id is not None else 0,
        "lotes_turnos_propuestos": 1 if proposed_id is not None else 0,
        "semanas_nocturnas": night_weeks, "bloques_turno_noche": night_blocks, "metas_activas": 4, "habitos": 3,
        "verificaciones_semanales": checks + hogar_checks, "chequeos_habito": habit_checks + hogar_habit_checks,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m body.demo")
    ap.add_argument("--home")
    ap.add_argument("--dias", type=int, default=DIAS_DEFAULT)
    ap.add_argument("--idioma", choices=("es", "en"), default="es")
    args = ap.parse_args(argv)

    if not args.home:
        _refuse("--home is required: pass a scratch folder for the demo (never the real BODY_HOME)")
        return
    home = Path(args.home)
    reason = _forbidden_home(home)
    if reason:
        _refuse(reason)
        return
    home_resolved = home.expanduser().resolve()
    home_resolved.mkdir(parents=True, exist_ok=True)
    db_path = home_resolved / "body.db"
    marker_path = home_resolved / MARKER_NAME
    if db_path.exists() and not marker_path.exists():
        _refuse(f"refusing: {db_path} already exists and is not a demo database (no {MARKER_NAME} marker) "
                 "-- pass an empty or demo-marked folder")
        return
    if db_path.exists():
        db_path.unlink()
    if marker_path.exists():
        marker_path.unlink()

    from . import config
    config.BODY_HOME = home_resolved
    config.DB_PATH = db_path
    config.TOKEN_DIR = home_resolved / "garmin_tokens"

    from . import db
    conn = db.connect()
    try:
        db.init(conn)
        counts = _seed(conn, dias=args.dias, idioma=args.idioma)
    finally:
        conn.close()

    marker_path.write_text(
        json.dumps({"seed": SEED, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}),
        encoding="utf-8",
    )

    print(f"demo home: {home_resolved}")
    for k, v in counts.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
