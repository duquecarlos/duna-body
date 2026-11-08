

## BODY-PULL-RELOJ-1 / BODY-SEMANAS-TARDIAS-1 -- the hosted daily pull asks each person for their
own today, not the server's; late data re-closes its own week (2026-11-08)

Two leftovers from BODY-SEGURIDAD-3-M, measured by the orchestrator on `main` at `572b3dd`:

**1. `BODY-PULL-RELOJ-1` itself: the server's date, not the person's.** `body/pull.py`'s
`pull_hosted` built the day list ONCE, from `date.today()`, and used that same list for every
person; `pull_backfill_hosted(conn, hoy=None)` defaulted `hoy = hoy or date.today()`, same
problem. The hosted daily pull's timer fires at one fixed instant (09:30 America/Bogota,
`deploy/host/body-pull.timer`): for a Bogota person the server's date already equals theirs at
that hour, so Carlos himself was never affected, but for a person in a zone far from the
server's, "today and yesterday" were the server's, not theirs -- the same clock rule
BODY-TIER1-LIMPIEZA-1 already named: a date that decides a business boundary is the PERSON's,
required, never a server default.

Fixed: `pull_hosted(conn, *, fecha=None, n_days=2)` -- `fecha` (from `--date`) stays one explicit
day for everyone, same as before; left out, EACH person's own day list is now built inside the
per-person loop from `fechas.hoy_local(db.person(conn, person_id))` and `n_days` (from `--days`).
`pull_backfill_hosted(conn, *, hoy=None)` -- `hoy`, when given (tests only), is still used for
every person alike; left out, each person's own `fechas.hoy_local` is read per person inside the
loop. No `date.today()` remains in either hosted path. `body/pull.py:364` (local mode's
`--days`/`--date` pull) and `body/pull.py:98` (`pull_all`'s starting point when nothing is
stored yet) both KEEP `date.today()` deliberately, with a comment saying why: local mode runs on
the person's own PC, so the PC's own clock already IS the person's clock -- there is no second
person's zone to get wrong, the same reasoning `vida_export`'s CLI already uses for its own
`date.today()`.

**2. A misnamed follow-up.** BODY-SEGURIDAD-3-M's own ledger entry (above, heading
`## BODY-SEGURIDAD-3-M -- ...`, its open-follow-up paragraph at `DECISIONS.md:8604`) recorded a
DIFFERENT finding under this same ID, `BODY-PULL-RELOJ-1`: "`cerrar_semanas` only re-closes the
current and previous week; data landing later than that ... never revisits that older
`goal_check` row." That finding is real, and is a distinct defect from the one above (a
one-shot-per-week limit vs. a wrong clock) -- it gets its own ID here, `BODY-SEMANAS-TARDIAS-1`,
rather than being folded into this one. (No correction is made to the inherited text at
`DECISIONS.md:8604` itself -- the ledger is append-only; this entry is the correction, pointing
at where the renaming happened.)

**`BODY-SEMANAS-TARDIAS-1` fixed.** `goals.cerrar_semanas(conn, person_id, *, hoy, desde=None)`
gained `desde`: when given, every week from `lunes_de(desde)` up to (not including) last week --
which `cerrar_semanas` already re-closes on its own -- is re-closed too. Three callers now supply
it:

- **Hosted `--backfill`** (`pull_backfill_hosted`): `_backfill_one_person` now returns `(days
  fetched, oldest day fetched this run)` instead of a bare count (and attaches `.oldest` to a
  raised exception alongside the existing `.saved`, so a mid-run failure still re-closes whatever
  it did fetch). `pull_backfill_hosted` collects these into `desde_por_persona: dict[int, date]`
  and passes it to `_cerrar_semanas_todas(conn, desde_por_persona)`, which now takes that mapping
  and passes each person's own `desde` (or `None`) to `_cerrar_semanas_seguro`. One run's 15-day
  batch (newest first) already reaches back far enough to cross into an older week than last
  week's, so this fires on ordinary backfill runs, not only a deep one.
- **Local `--all`** (`pull_all`): now also returns `(days saved, oldest day saved this run)`;
  `_run` passes that oldest as `desde` to the single `_cerrar_semanas_seguro` call shared by
  `--all`, `--recompute` and the default/`--days`/`--date` pull (the other three paths pass
  `desde=None`, since none of them can land further back than last week).
- **A late habit-check correction** (`body/rutas/metas.py`'s `habit_check` POST): after
  `check_habit` stores the day, if that day's week (`fechas.lunes_de(day)`) is older than last
  week, the route itself calls `goals.weekly_check(conn, goal_id, person_id, week_start)` for that
  one week, in the same request -- a POST, so it may write; a day in this week or last is left to
  the daily pull, which already owns both, so nothing is written for those (kept green by the
  unchanged `tests/test_get_sin_escritura.py` GET-write guard, since this route was already a POST).

**Tests**, gate run once on this branch's final tree: `.venv/Scripts/python.exe -m unittest
discover tests -t .` -- `Ran 1301 tests in 164.880s ... OK (skipped=1)` (1294 before this slice,
+7 new): `tests/test_garmin_host.py`'s new `PullHostedPerPersonZoneTest` (2: a person in
`Pacific/Auckland` and one in `America/Bogota` each asked for their own today/yesterday from one
frozen instant where the two dates differ, via `fechas.datetime` patched the same way
`tests/test_fecha_futura_1.py` already does; the same for `pull_backfill_hosted` with no `hoy`
given); `tests/test_primer_pull_1.py`'s `WeeklyChecksHostedTest.
test_backfill_passes_the_oldest_stored_day_as_desde_so_old_weeks_reclose` (one backfill run's
15-day batch passes its oldest day as `desde`); `tests/test_goals.py`'s `CerrarSemanasTest` gained
`test_desde_recloses_the_older_weeks_and_no_others` and
`test_desde_on_or_after_last_monday_adds_nothing` (exact week set, nothing further back than
asked); `tests/test_goals_web.py`'s new `HabitCheckLateCorrectionTest` (a check POSTed for a day
three weeks back updates that week's stored `habits_done`; a check for the current week writes no
`goal_check` row at all).

**Gate**, run once on this branch's final tree: `.venv/Scripts/python.exe -m unittest discover
tests -t .` -- `Ran 1301 tests in 164.880s ... OK (skipped=1)`.

**Merge.** Commit `0bcb9e8b697a2c7dc3fd69de106129b6eca16e9e` on branch `feat/body-pull-reloj-1`,
base `main` at `572b3dd812faeb73d32e81791bcfdd7d5d5af8f6`. `git merge --no-ff
feat/body-pull-reloj-1` onto `main`, producing merge commit
`1601702069d04e5ae0425a552c7c72c28abe7b4d`. Merge tree `0554b32ad84b8ddabb9feed8dc40d7cf2591b633`
equals the branch tip's own tree exactly -- no conflicts, no change beyond the branch's own
content. Diff stays inside Tier 2 (`body/goals.py`, `body/pull.py`, `body/rutas/metas.py`, and
four test files): no schema or migration change; no customer-facing bytes (no template or UI
string touched; the only observable difference is which days the hosted pull asks Garmin for --
identical for Carlos's own zone at 09:30 -- and which `goal_check` rows a late correction
recomputes); no cross-repo contract. The spec's own approval-reason ("nothing changes on Carlos's
machine beyond which days the hosted pull asks for ... This spec approves the merge.") also
authorises the merge in words. Merges on green rather than stopping AWAITING_APPROVAL. No
password, token, session secret or health figure was read, printed or logged by this slice.
`git status --porcelain` is empty on `main` after this merge, before this ledger entry's own
commit.
