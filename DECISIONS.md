# Decisions

This is body-app's ledger: one entry per slice, appended in order, never edited or removed. It records what each
slice changed and why, so a later session can see the repository's history of decisions without reading every diff.

## BODY-REPO-RULES-1 -- CLAUDE.md becomes the product-repo governing file (2026-11-06)

`CLAUDE.md` was written by the session that was both this product's builder and its orchestrator, so it still read
"This session is the Body app orchestrator" and "First steps: propose the stack". Both sections were removed; that
content is done (the stack was proposed and accepted) or has moved to the new orchestrator at
`C:\Duna\body\dev-protocol`. In their place, `CLAUDE.md` now says in one short section that this repository is worked
by slices dispatched from that orchestrator, with the plan in `ROADMAP.md`, the design rules in `docs/DESIGN.md` and
the ledger in this file. The opening section (Carlos's idea, "Body app", for him first and a Duna product later, the
template-not-one-person rule) and the starting-direction bullets (the Garmin adapter, the official Health API later,
shifts via Personal, what the app does, health data is sensitive, his phone) were kept unchanged in substance.

Added sections: **Privacy** (never type, store, read or ask for a password; only the token lives in `BODY_HOME`; no
health figure in a URL, log, commit, ledger entry or report; no third-party analytics; mocks use invented demo data),
**Tests never touch real data** (every test module imports `tests/_isolate.py` first), **The gate** (the exact command
below; pytest is not installed in `.venv`, so `unittest` is the runner), **The trunk is `main`** (slices branch from
and merge back to `main` with `--no-ff`), and **Tier**.

Tier 1 list, as written in `CLAUDE.md`: the Garmin adapter `body/garmin_adapter.py` (the only module that may import
garminconnect), Garmin sign-in and token handling `body/login.py`, where health data and the token live
`body/config.py`, the SQLite schema and its migrations `body/db.py`, the export that hands health data to another app
`body/vida_export.py`, and the test isolation guard `tests/_isolate.py`.

Measurement behind that list: grepping `body/` for imports of `garminconnect`, `httpx`, `requests`, `urllib`,
`socket` and `smtplib` found exactly one network-capable import outside the adapter -- `from urllib.parse import
parse_qs` at `body/web.py:14`, which is query-string parsing, not network egress, so it added nothing. `garminconnect`
is imported only at `body/garmin_adapter.py:36` and `body/garmin_adapter.py:49`. `body/login.py:9` imports only
`garmin_adapter` (plus stdlib `getpass`); `body/pull.py:15` imports `db`, `garmin_adapter` and `readiness`, none of
them new egress. No write outside `BODY_HOME` was found (`body/config.py:5-7` defines `BODY_HOME`, `DB_PATH` and
`TOKEN_DIR`; `body/vida_export.py:71` writes to a caller-supplied path under that home). The only non-loopback bind is
`body/web.py:360`, `host = "0.0.0.0" if a.lan else "127.0.0.1"`, driven by the `--lan` flag documented at
`body/web.py:1,4,357`. Per the spec, `body/web.py` is NOT added to Tier 1 -- every screen in the app lives there, so
making the whole module Tier 1 would stop ordinary UI slices for a LAN-exposure concern that lives in a few lines of
it. See `open_followups` below.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 12 tests, OK, on this slice's final tree (no code
changed by this slice).

Follow-up proposed, not done here: extract the `--lan` / listen-host choice out of `body/web.py` into its own small
module, so that LAN exposure becomes a Tier 1 path of its own instead of being buried inside the file every UI slice
touches.

## BODY-WEB-LISTEN-HOST-1 -- listen-host choice moves into its own module (2026-11-06)

Closes the BODY-REPO-RULES-1 follow-up above. The `--lan` listen-address decision (`host = "0.0.0.0" if a.lan else
"127.0.0.1"`, plus the start-up print) moved out of `body/web.py`'s `main()` into a new, dependency-free module
`body/listen.py`, which owns exactly that one decision. It exposes `LOCAL = "127.0.0.1"`, `listen_host(lan: bool) ->
str` (`LOCAL` unless `lan`, then `"0.0.0.0"`), and `describe(lan: bool, port: int) -> str` (the start-up line: the
local URL, or -- for LAN -- a line saying the app is OPEN TO EVERY DEVICE ON THIS NETWORK with the placeholder
`<this-PC-IP>`, never the literal `0.0.0.0`). `body/web.py`'s `main()` now calls `listen.listen_host(a.lan)` and
`listen.describe(a.lan, a.port)`; `access_log=False` and `log_level="warning"` are unchanged.

Grep for `0.0.0.0` / `127.0.0.1` in `body/web.py` after the change found exactly one remaining hit: the module
docstring's `http://127.0.0.1:8770` example at `body/web.py:1`, which stays because it is documentation of the local
URL, not a listen-address decision.

`tests/test_listen.py` imports `tests/_isolate.py` first, then checks `listen_host(False)` / `listen_host(True)`,
that both `describe` lines carry the port and never the text `0.0.0.0` (decided here: a bind-all address is not a URL
anyone can open, so the LAN line shows `<this-PC-IP>` instead), and that the LAN line differs from the local one.

`CLAUDE.md`'s Tier 1 paragraph gained one item: "which network address the web app listens on `body/listen.py`",
kept as one comma-separated paragraph.

## BODY-TURNOS-IMPORT-1 -- schema, propose/confirm, the Turnos confirm card (2026-11-06)

Carries out BODY-TURNOS-IMPORT-OBS-1 (OBSERVED, 2026-11-06), approved by the orchestrator under
ORCH-CARLOS-DELEGATES-TIER-1-1. Stage two of the Turnos import: the shift files Personal extracts from Carlos's
schedule email (format in its `FORMATO.md`) can now be proposed and confirmed into `shift`, never written silently
(docs/DESIGN.md rule 1).

**Schema (`body/db.py`, additive only, as written):**
```
CREATE TABLE IF NOT EXISTS shift_batch (
    id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES person(id), file_name TEXT NOT NULL,
    received TEXT NOT NULL, extracted_at TEXT NOT NULL, source_person TEXT, source_timezone TEXT,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','confirmed','dismissed','superseded')),
    day_count INTEGER NOT NULL, created_at TEXT NOT NULL, decided_at TEXT,
    UNIQUE (person_id, file_name, received, extracted_at)
);
CREATE TABLE IF NOT EXISTS shift_batch_item (
    id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES shift_batch(id), day TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('morning','afternoon','night','day','off','other')), code TEXT,
    start_local TEXT, end_local TEXT, holiday INTEGER NOT NULL DEFAULT 0, sunday INTEGER NOT NULL DEFAULT 0,
    note TEXT, outcome TEXT CHECK (outcome IN ('applied','manual_kept','newer_kept') OR outcome IS NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_shift_batch_item_slot ON shift_batch_item (batch_id, day, COALESCE(start_local, ''));
```
`_migrate()` adds `shift.batch_id INTEGER REFERENCES shift_batch(id)` behind the existing guarded-ADD-COLUMN
check, then `CREATE UNIQUE INDEX IF NOT EXISTS idx_shift_slot ON shift (person_id, day, COALESCE(start_local, ''))`
-- the index BODY-TURNOS-IMPORT-OBS-1 found missing, which is why two off-day rows (`start_local` NULL) for the
same person and day did not collide on the primary key alone. **No DELETE, ever, in the migration**: the index
creation is wrapped in `try/except sqlite3.IntegrityError: pass`, leaving any colliding rows untouched. The
missing-index state is made visible by `body.shift_import.slot_index_ok(conn)`, and `confirm()` raises
`SlotIndexMissing` and refuses to write until the index exists.

**`body/shift_import.py` (new, added to Tier 1):** `propose(conn, path, person_id)` validates the whole file
before any write and is a no-op (returns the existing batch id) on a repeat of the same
`(person_id, file_name, received, extracted_at)`, dismissed or not. A batch's precedence is the pair
`(received, extracted_at)` compared in that order. Refusal rules enforced, each refusing the whole file with
zero rows written: unknown `kind`; malformed `day`; `start_local`'s date not matching `day`; non-null
`start_local` on an `off` row or null `start_local` on a non-`off` row; two entries with the same `day` and the
same `start_local` (null counts as one slot); an `off` entry and a working entry on the same `day`; a missing
`source.received` or `source.extracted_at`. Two working entries on one day with different `start_local` are
allowed. `confirm(conn, batch_id)` runs day by day in one transaction: a day with a `manual` row in `shift` is
left alone (item outcome `manual_kept`); a day already carrying a newer confirmed batch's rows (by the
precedence pair) is left alone (`newer_kept`); otherwise that day's `source LIKE 'personal:%'` rows (never a
manual row) are deleted and the batch's items inserted with `source = 'personal:<file_name>'` and `batch_id`
(`applied`). A previously confirmed batch whose every applied day has since been replaced is marked
`superseded`. `dismiss(conn, batch_id)` retires a `proposed` batch. CLI: `python -m body.shift_import [--dir PATH]
[--person-id N]`, reading `BODY_SHIFTS_DIR` itself (no default path in code), proposing every `turnos-*.json` in
`(received, extracted_at)` order and printing counts only (files read, batches proposed, already known, refused)
plus the refusing rule and day for each refusal -- never other file content. A `codigos.json` sidecar is ignored
by the glob itself.

**Web:** `/shifts` (new `body/templates/shifts.html`) shows a confirm card -- the mail's received date, day and
night counts, and how many days a manual edit will keep -- with «Confirmar»/«Descartar» POSTs to
`/shifts/confirm` and `/shifts/dismiss` (`batch_id` only, the `/settings/language` parse_qs + 303 pattern); with
no proposed batch it keeps `soon.html`'s content plus the last confirmed batch's date if one exists. Hoy's static
shift-none line becomes a link to `/shifts` ("Llegaron tus turnos: revísalos →" / "Your shifts arrived: review
them →") when a batch is proposed, otherwise unchanged. Every new string is in both `body/i18n/es.json` and
`en.json`. `CLAUDE.md`'s Tier 1 paragraph gained "the import that brings another app's shift files in
`body/shift_import.py`", kept as one comma-separated paragraph.

**Tests:** `tests/test_shift_import.py`, 25 tests, plus two invented fixtures (`turnos-demo-a.json`,
`turnos-demo-b.json`; b's `received` is later and overlaps two of a's four days). Covers: idempotent re-propose;
a same-`received`-later-`extracted_at` re-extraction winning after confirm; each decision-3 refusal rule with
zero rows written; two slots on one day; newer-wins and older-marks-`newer_kept`; manual row kept; superseded;
dismiss; the slot-index regression via raw SQL; the `IntegrityError` path leaving rows untouched and blocking
confirm; `/shifts` and Hoy in both languages; the confirm/dismiss POSTs; the CLI with no folder configured.

**Gate:** `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 58 tests, OK, on this slice's final tree.

Stopped on the branch before merging (`feat/body-turnos-import-1`, slice commit `18d92bc1b75fe05a364a9262bce332583d5f88d1`):
a schema change and new customer-facing strings both trip merge policy A, and the spec's own GATE section says to
stop here rather than merge. The orchestrator merges.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 16 tests, OK (12 before this slice, 4 added
here).

## BODY-HISTORIA-1 -- «Tu historia»: the whole history read as patterns (2026-11-06)

A new read-only drill-down, reached from Tendencias, answering "what does my history SAY about me?" -- the base
the Metas conversation will start from. One new module, no schema change (read-only through `db.connect()`,
plain SELECTs; no new table, column or index was needed).

`body/history.py` (pure functions over a connection and a person id, no web imports, returning plain dicts;
formatting -- units, measured/estimated, translated labels -- happens in `body/web.py`, the same split
`profile.py`/`baselines.py` already use):
- `coverage(conn, person_id)`: first/last day with watch data (from `db.days_with_data`), days present, days
  missing (span minus present, an honest gap, never implied as 0 activity), the longest gap's length and its
  own start/end dates, and the activity count.
- `training(conn, person_id)`: activities grouped by Garmin `type`; the top 5 by total time individually
  (`TRAINING_TOP_TYPES = 5`), the rest merged into "other"; the same month-by-type minute breakdown (for a
  stacked bar); the usual week (median sessions/week over weeks that had one); the "longest" (most cumulative
  time) and "most frequent" (most sessions) kind; a year-over-year compare of the last 90 days against the
  same 90 days a year before the most recent activity, `None` (not 0) when there is nothing from a year ago.
  An unknown `type` key never crashes: `KNOWN_ACTIVITY_TYPES` lists the Garmin keys this app has an
  `activity.type.<key>` label for; anything else carries `known: false` and a `fallback_label` (the raw key,
  underscores to spaces, capitalised), which `body/web.py` shows instead of a translation.
- `patterns(conn, person_id)`: weekday profile (mean) and month/season profile (median across years) for
  `sleep_minutes`, `resting_hr`, `hrv_last_night`, `stress_avg`, `body_battery_wake` -- the same metric keys
  Tendencias already carries in `METRIC_DEFS` -- each bucket with the person's own p25-p75 band
  (`statistics.quantiles`), computed only when a bucket has `PATTERN_MIN_N = 4` or more values. Up to
  `MAX_FINDINGS = 3` plain-language findings are chosen by: for each metric/axis, if the bucket spread clears
  a noise floor, take the "worse" bucket (max when `better == 'lower'`, else min -- **the same rule
  `body/web.py`'s existing `trends()` already uses to bold the worst weekday in its week table**, re-found and
  reused rather than re-invented) and rank it by `spread / population-stdev-of-the-whole-metric-history`
  (normalises different units so HRV ms and sleep minutes compare on the same footing); ties break by
  `PATTERN_KEYS` order then weekday-before-month. The noise floor itself is `body/web.py`'s existing
  `trends()` week-highlight constant, copied in as `NOISE_FLOOR_MINUTES = 5.0` / `NOISE_FLOOR_DEFAULT = 1.0`
  (its literal `1 if not _is_minutes(k) else 5`).
- `by_shift(conn, person_id)`: the same five metrics' means grouped by shift `kind`, for days that have a
  `shift` row (one kind per day; a day with more than one shift row keeps the first). No shift rows at all
  returns `{"has_shifts": False, "kinds": []}`, which `body/web.py`/`history.html` render as an honest empty
  card pointing at Turnos, per rule 6 (honest gaps) and rule 7 (shifts are the axis).

Route: `GET /history` in `body/web.py`, rendering `body/templates/history.html` (tab stays "trends" -- this is
a drill-down from Tendencias, not a fifth bottom tab, per the spec). Hero = the coverage line plus the single
highest-impact finding (or an honest "not enough history yet" when there are none). Sections: Entrenamiento
(the stacked monthly bar -- `.stage`/`.legend`, the same CSS primitives `day.html` already uses for sleep
stages, reused rather than a new chart library, since this app has no existing bar-chart style and a line
chart does not fit a type breakdown; every month links to `/trends?m=YYYY-MM`, which already drills to days
and then to `/day/<day>`), Patrones (weekday/month tables styled like Tendencias' existing week table, the
matching cell bolded when a finding names it, plus the finding sentences), Por turno (or its empty card). One
link added to `trends.html` ("Tu historia completa →" / "Your full story →"), styled `.more` like the existing
`today.see_day` link; no fifth tab was added to `base.html`.

i18n: every new string added to both `es.json` and `en.json` (`history.*`, `shift.kind.*`, `activity.type.*`
for every key in `KNOWN_ACTIVITY_TYPES`, plus `trends.history_link` and the two missing short labels
`trends.short.stress_avg` / `trends.short.body_battery_wake` the existing week table never needed before).
`body/static/body.css` gained three rules extending the existing `.shift.night`/`.shift.day` pattern to
`morning`/`afternoon` (amber, like `day`) and `off`/`other` (neutral wash), used by the by-shift cards.

`tests/test_history.py` imports `tests/_isolate.py` first. Invented demo data: ~4 months (Sept-Dec 2025) of
daily `resting_hr`/`hrv_last_night`/`sleep_minutes` with a deliberate 3-day gap (Nov 5-7), a Saturday-only
resting-HR wobble (+6 bpm, clears the 1.0 floor) and a near-flat HRV wobble (+0.2 ms, stays under the 1.0
floor on purpose, to test exclusion), December sleep 70 min lower (clears the 5-min floor); six `activity`
types (one, `paddle_boarding`, not in `KNOWN_ACTIVITY_TYPES`, to test the fallback; one, `elliptical`, sized
to be the smallest and merge into "other"); shift rows inserted by hand with plain SQL against `db.py`'s
schema (no existing test module had inserted a `shift` row before, so there was no prior pattern to follow).
Covers: coverage's dates/gap/activity-count; training's grouping, the unknown-type fallback, the
smallest-type-merges-into-"other" case, and the year-over-year `None` with no data a year back; a weekday
finding above the floor (resting HR, Saturdays) and one below it excluded (HRV); a month finding (sleep,
December); `by_shift`'s empty and populated states. The route test uses the same `fastapi.testclient.TestClient`
+ `POST /settings/language` pattern `tests/test_pipeline.py`'s `test_end_to_end_page` already established (no
new pattern invented), checking `/history` returns 200 with the right title text in both languages.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 33 tests, OK (16 before this slice, 17
added here).

## BODY-TURNOS-IMPORT-1-M -- land BODY-TURNOS-IMPORT-1 (2026-11-06)

BODY-TURNOS-IMPORT-1 stopped on `feat/body-turnos-import-1` (branch tip `ce0b9bf6b3fe35300c80f7b4f8321387bcb7c677`)
AWAITING_APPROVAL, tripping merge policy A on a schema change and new customer-facing strings, per the GATE section
of its own spec. This slice carries no code change: approved by the orchestrator under
ORCH-CARLOS-DELEGATES-TIER-1-1 after reviewing the diff (13 files, +901/-6) against `main` -- the schema additions
in `body/db.py` (`shift_batch`, `shift_batch_item` with its slot index, `shift.batch_id` via the guarded ADD
COLUMN, and `idx_shift_slot` inside a try/except IntegrityError with no DELETE), `body/shift_import.py`'s one
DELETE (a day's `source LIKE 'personal:%'` rows inside `confirm()`, never a manual row), the confirm card, the Hoy
notice, the i18n pair and the `CLAUDE.md` Tier 1 item, all as specced -- and merged unchanged.

`main` had not moved since the branch was cut: `git merge-base main feat/body-turnos-import-1` and `git rev-parse
main` both resolved to `6f487efecdd43bbfd9670fbaf131508de705e1b9`, so no merge of `main` into the branch was
needed.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 58 tests, OK, run once on the branch tip
before merging.

Merge commit: see `git log -1 main` after this entry.

## BODY-TURNOS-SYNC-1 -- shifts confirmed with one tap however many files arrived (2026-11-06)

Extends `body/shift_import.py` within the design BODY-TURNOS-IMPORT-OBS-1 measured and BODY-TURNOS-IMPORT-1 built,
approved by the orchestrator under ORCH-CARLOS-DELEGATES-TIER-1-1. Carlos's first import is about 33 files spanning
several years at once, and confirming each one separately was the failure this fixes.

`scripts/install-daily-sync.ps1` gains an optional `-ShiftsDir <folder>` parameter. Only when it is given, "Body app
daily sync" gets a second action, `python -m body.shift_import --dir "<folder>"` (quoted, so a path with spaces
works), run after the Garmin pull, working directory the repo. No default path lives in the script -- the folder is
always the installer's own argument, per the template rule that goals/metrics/plans (and now the shifts folder) are
data, never a person's path hardcoded into the app. `-Force` is kept so re-running replaces both tasks. The final
`Write-Host` now reports whether shift import is on and from which folder.

`body/shift_import.py` gains two functions:
- `confirm_all(conn, person_id)` -- every `proposed` batch of this person, in `(received, extracted_at, id)` order,
  each through the unchanged `confirm()`. **One transaction per batch, not one for the whole set**: `confirm()`'s
  own `with conn:` commits as soon as it returns, so nesting it inside a second `with conn:` would not make the set
  atomic without changing `confirm()`'s own transaction handling, which stayed out of scope here -- precedence,
  manual-kept and the no-DELETE-of-manual rule are exactly what `confirm()` already enforces, one batch at a time.
  Refuses up front, writing nothing, when the slot index is missing (the same `SlotIndexMissing` guard `confirm()`
  applies). Returns counts: batches, days applied, days kept because a manual edit covers them, days kept because a
  newer file covers them.
- `summary_proposed(conn, person_id)` -- read-only: how many batches are waiting, the day range they span (min/max
  `day` across their items), and the total distinct days/nights across all of them, for the one combined card.

`body/web.py`'s `GET /shifts`: when more than one batch is `proposed`, renders one card -- "Llegaron N archivos de
turnos · <mes año> → <mes año> · D días, K noches" (`t.month_year` on the earliest/latest day from
`summary_proposed`) -- with a "Confirmar todos" button posting to the new `POST /shifts/confirm-all` (which calls
`confirm_all` and 303s back to `/shifts?done=1&...` carrying the counts as plain integers in the query string, not
health figures, so the page can show a one-line result: "Confirmaste N archivos: D días aplicados.", plus a kept-days
note when any days were kept) and a `<details>` "Ver archivo por archivo" listing each batch with its own existing
confirm/dismiss. With exactly one batch, the original single-batch card is unchanged. Hoy's notice now says
"Llegaron N archivos de turnos: revísalos →" when more than one batch is proposed (unchanged wording for exactly
one). Every new string added to both `body/i18n/es.json` and `en.json` (tú register, rule 1 "data arrives by itself;
you only confirm"); `body/static/body.css` gained three rules for the `<details class="files">` tap target (44 px,
rule 11) reusing the existing `.card`/`.k` look rather than a new component.

`tests/test_shift_import.py`: `confirm_all` over three invented batches proposed out of order applies in
`(received, extracted_at)` order and produces the exact same `shift` rows as confirming them one by one; refuses
with the slot index missing (writes nothing, batches stay `proposed`); a manual day survives it; `summary_proposed`'s
counts (including the all-proposed-empty case). Web: `/shifts` with 0, 1 and 3 proposed batches in both languages
(the 3-batch case spans November→December, checked against `summary_proposed`'s day/night counts); `POST
/shifts/confirm-all`. The installer is read as text, never executed: a test asserts `$ShiftsDir` exists, that
`body.shift_import` appears only inside the `if ($ShiftsDir)` branch, and that no personal name or `Users` path
appears in the script.

No schema change (no `touches` on `body/db.py`); no cross-repo contract change. **Customer-facing bytes did change**
(new confirm-all card, new i18n strings, the Hoy notice's many-files wording), which trips merge policy A's
customer-bytes condition, as BODY-TURNOS-IMPORT-1 itself did before it. The spec's own GATE section says to stop
before merging in that case, so this slice stops on `feat/body-turnos-sync-1` AWAITING_APPROVAL rather than merging,
per `BODY-TURNOS-IMPORT-1-M`'s precedent for a later merge slice to review and land.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 66 tests, OK (58 before this slice, 8 added
here), run once on the final branch tree before this entry.

## BODY-TURNOS-SYNC-1-M -- land BODY-TURNOS-SYNC-1 (2026-11-06)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after reviewing the diff (10 files, +384/-16)
against `main`; merged unchanged. No code change in this slice.

`main` had not moved since the branch was cut: `git merge-base feat/body-turnos-sync-1 main` and `git rev-parse main`
both read `e8498159a258e0e7a4fa817944e848b4eb9a5c18`, matching the branch's recorded base, so no merge-forward was
needed.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 66 tests, OK, run once on the branch tip
(`65cecb7e3121c5c630905cdbcb59cbdf799c7112`) before this merge.

Merge commit: recorded in the next entry's git history (this entry precedes the `--no-ff` merge, per
`BODY-TURNOS-IMPORT-1-M`'s precedent).

## BODY-WEB-AUTOSTART-1 -- the web app runs without a hand step (2026-11-06)

Carlos wants his hand steps close to zero: today the web app ran only when someone started it, so after the shift
import proposed 33 batches there was no current server for him to confirm them in. `scripts/install-daily-sync.ps1`
now registers a third task, "Body app web", alongside the existing daily sync and weekly file.

The task runs `-m body.web --port $Port` (new `[int]$Port = 8770` parameter), plus ` --lan` only inside the
`if ($Lan)` branch (new `[switch]$Lan`); local-only is the default -- opening it to the home network is one
explicit flag, never the default, per `body/listen.py`'s own local-first rule. Its trigger is
`New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME`, its own settings object (`$webSettings`, never the shared
`$settings` the other two tasks use, since this one must not wait on a network condition to start): no network
condition, `-ExecutionTimeLimit ([TimeSpan]::Zero)` so it runs indefinitely, `-RestartCount 3 -RestartInterval
(New-TimeSpan -Minutes 1)` so a crash recovers on its own, `-MultipleInstances IgnoreNew` so a logon during an
already-running session cannot start a second copy. Right after `-Force` registering, any already-running instance
of the task is stopped (`Stop-ScheduledTask`, `-ErrorAction SilentlyContinue` for "not running") and then started
again (`Start-ScheduledTask`), so a re-install picks up new code immediately rather than waiting for the next
logon. The action uses `pythonw.exe` from the venv when it exists there (no console window popping up at logon),
falling back to `python.exe` otherwise -- `pythonw.exe` was present in this repository's `.venv\Scripts`, so that
is the executable the registered task actually uses. The header comment and the three-task remove line were
updated to match; the final `Write-Host` gained a line with the local URL and, only when `-Lan` is passed, the
plain warning that every device on the home network can open it.

No `body/listen.py` or `body/web.py` change -- the task only ever passes the flags those modules already accept
(`--lan`, `--port`), so Tier 1 is untouched.

`README.md` gained a short "Keep it running" section: the install line (with and without `-Lan`), and what each of
the three tasks does, no personal path.

`tests/test_installer.py` (new, 6 tests, `tests/_isolate.py` imported first): reads the `.ps1` as text and asserts
the web task's trigger assignment contains `New-ScheduledTaskTrigger` and `-AtLogOn`, and that its
`Register-ScheduledTask` call passes `-Trigger $webTrigger`; `--lan` appears only inside the `if ($Lan) { ... }`
block and nowhere else in the file; the literal `0.0.0.0` never appears; no `C:\Duna` or `C:\Users` absolute path
appears; the web task's `Register-ScheduledTask` call uses `-Settings $webSettings`, never `-Settings $settings`;
the `# Remove with:` comment line names all three task names.

No schema or migration change; no cross-repo contract change; the installer's console output and the README are
not customer-facing application bytes (no screen, no i18n string). Merges on green per merge policy A.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 97 tests, OK (91 before this slice, 6 added
here), run once on the final branch tree before this entry.

## BODY-HISTORIA-POLISH-1 -- round 1 polish of «Tu historia» (2026-11-06)

The orchestrator measured `/history` at 375 px on real data after BODY-HISTORIA-1 (`6f487ef`) and found four shapes
worth fixing: the hero sentence read "Your toughest Resting heart rate: December (...)" -- the metric label kept its
title case mid-sentence and said nothing about which direction was bad, and the same sentence then repeated in the
Patterns findings below it; the training legend showed "0.0 km" for a type that never logs a distance (strength
training); the training card listed every month since the first, oldest first, making the page roughly 3,600 px tall
with the legend after all of it.

**Findings as sentences.** `web._finding_text` no longer builds "Your toughest {label}": it now picks one of four
templates by axis and by the metric's own `better` direction -- `history.find.weekday_better_lower`,
`history.find.weekday_better_higher`, `history.find.month_better_lower`, `history.find.month_better_higher` (both
`es.json` and `en.json`) -- and a new `metric.<key>.sentence` label (lower case, e.g. "pulso en reposo" /
"resting heart rate") in place of the title-case `metric.<key>` label used elsewhere. A "lower is better" metric
(`resting_hr`, `stress_avg`) reads as running highest where it is worst ("Tu pulso en reposo sube más los sábados" /
"Your resting heart rate runs highest on Saturdays"); a "higher is better" metric (`sleep_minutes`, `hrv_last_night`,
`body_battery_wake`) reads as running lowest. `history_view` now builds the hero from `pat["findings"][0]` and passes
only `pat["findings"][1:]` to the Patterns card, so the hero sentence is never repeated below.

**Distance only where it means something.** `history.training`'s `_type_row` and its per-month aggregation computed
`km` from `distance_rows` alone (whether a row ever carried a non-null `distance_m`), so a type whose every session
reports an explicit `distance_m = 0` (as Garmin does for strength training) still produced `0.0 / 1000 = 0.0`, read as
truthy by the template's `{% if ty.km %}`. Both now also require `distance_m > 0`, so a zero or null total comes back
as `None`, the same honest gap the rest of this module already uses.

**Training list newest first, 12 months, legend moved up.** `web._training_ctx` reverses the month list `training()`
builds (oldest first, for the stacked-bar `max_month_minutes` math) before handing it to the template, then splits it
at `TRAINING_RECENT_MONTHS = 12`: the recent 12 render directly, the rest sit inside a `<details class="files">` --
"Ver todos los meses" / "See every month" -- reusing the tap-target rule BODY-TURNOS-SYNC-1 already added for
`body.css`'s `details.files summary`, so no new CSS was needed here. The legend moved in `history.html` from after
the month list to directly under the card's question, above the bars.

`tests/test_history.py`: a `strength_training` activity with `distance_m = 0` on every session (sized to outrank
`hiking` for a top-5 slot, so the old bug's exact shape -- a zero distance inside the legend, not merged into
"other" -- is reproduced and fixed); `FindingTextTest` checks both directions in both languages and that the
title-case label never appears mid-sentence; `TrainingCtxOrderTest` checks `_training_ctx` reverses and splits at 12
against a synthetic 14-month list (the seeded fixture only spans 4 months); `HistoryRouteTest` gained a check that the
hero sentence's exact text appears exactly once in the rendered page. `test_smallest_type_merges_into_other` and
`test_grouping_and_unknown_type_fallback` were updated for the new activity's effect on the top-5 ranking.

No schema change (no `touches` on `body/db.py`); no cross-repo contract change. Customer-facing bytes did change (new
sentence templates, new `*.sentence` labels, the moved legend, the `<details>` reveal) -- the spec's own GATE section
instructs `Merge --no-ff into main` unconditionally on green, which this entry reads as the body's explicit
authorization to merge past that condition, per the merge-policy-A rule that a spec's body can say so in words.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 71 tests, OK (66 before this slice, 5 added
here), run once on the final branch tree before this entry.

## BODY-METAS-1 -- goal/habit/goal_check: vida-v1 shapes for Metas' data half (2026-11-06)

Carries out `BODY-METAS-OBS-1`, approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1`. The
investigation's findings re-verified unchanged: `goal`'s CHECK had no `'proposed'`, `plan_item`/`weekly_check` had
zero writers anywhere and one reader (`vida_export.week_section`'s goals list), `test_household.py` asserted only
`goal.hogar_id`. No real goal, habit or weekly-check row existed anywhere this slice could touch; the migration test
builds the old shape from scratch. Seeds no goal of Carlos's.

**Tables, in `body/db.py`.** `goal` widened to vida-v1's shape: `area` (six-value closed list, default `'body'`),
`why`, `kind` (`metric` | `milestone`, default `'metric'`), `direction` (`raise` | `lower` | `keep`, nullable),
`visibility` (`private` | `household`, default `'private'`), and `status`'s CHECK widened to add `'proposed'`
(DDL default stays `'active'`; `goals.propose()` sets `'proposed'` explicitly in code, not the DDL, per the
orchestrator's decision). A table-level CHECK ties `kind` to its required fields: `metric` needs `metric_key` and
`direction`; `milestone` needs `deadline` and no `metric_key`. `plan_item` -> `habit` (`cadence` now `TEXT NOT NULL
CHECK (json_valid(cadence))`, `cadence_text_legacy` added, `shift_context` folded into the JSON's `context` key).
`habit_check(habit_id, person_id, day, done, note)`, new, `PRIMARY KEY (habit_id, person_id, day)`. `weekly_check` ->
`goal_check(id, goal_id, person_id, week_start, metric_avg, on_track, habits_done, habits_due, rule_version, note)`,
`UNIQUE (goal_id, person_id, week_start)` (person added; a weekly rule version travels with every row). `plan(id,
person_id, hogar_id, goal_id NULL, day, time, title, booking JSON, status)`, new. `_GOAL_COLUMNS`/`_HABIT_COLUMNS`/
`_GOAL_CHECK_COLUMNS` are shared string constants between `SCHEMA` (fresh installs) and the rebuild functions
(existing installs), so the two paths cannot drift apart.

**Rebuild procedure and its guard.** `_migrate_metas()` runs before `SCHEMA`'s `executescript` (not after, as the
rest of `_migrate()` runs): the new table names (`habit`, `goal_check`) differ from the old (`plan_item`,
`weekly_check`), so if `SCHEMA` ran first it would create an empty `habit`/`goal_check` before the rebuild had a
chance to populate them from the old data. Each rebuild is guarded to fire once: `goal` rebuilds when its
`sqlite_master.sql` text lacks `'proposed'`; `habit` rebuilds when `plan_item` exists and `habit` does not;
`goal_check` rebuilds when `weekly_check` exists and `goal_check` does not. `_rebuild_with_fk_off()` wraps each
rebuild in one explicit transaction (`PRAGMA foreign_keys = OFF` before an explicit `BEGIN`, per SQLite's documented
12-step ALTER procedure): create the new table, copy every row through a per-row function (`_copy_goal_row` /
`_copy_habit_row` / `_copy_goal_check_row`, each a module-level function so a test can intercept one row), compare
`count(*)` old vs new and run `PRAGMA foreign_key_check`, and only then `DROP` the old table and `RENAME`/commit; any
mismatch, violation or exception rolls back the whole transaction (including the `CREATE TABLE`) and raises, leaving
the old table exactly as it was. Legacy `goal` rows: `kind='metric'` when `metric_key` was set (direction inferred
from that metric's `metric_def.better` -- `higher`->`raise`, `lower`->`lower`, `neutral`->`keep`, since the old shape
never recorded direction), else `kind='milestone'`; `area='body'`, `visibility='private'`, `why=NULL` for every
copied row. Legacy `plan_item.cadence` free text: `'daily'` -> `{"per":"day","times":1}`, `'Nx/week'` (case
insensitive) -> `{"per":"week","times":N}`, anything else -> `{"per":"week","times":1}`; the original text is kept in
`cadence_text_legacy`; `shift_context` becomes the JSON's `context` key. Legacy `weekly_check` rows take their
goal's `person_id` and `rule_version='legacy'`.

**`body/goals.py`** (Tier 2: no schema, sends nothing anywhere). `propose`/`activate`/`pause`/`finish`/`drop` are
thin status setters over `goal`; `add_habit`/`check_habit` (upsert on `(habit_id, person_id, day)`). `weekly_check(
conn, goal_id, person_id, week_start)` returns `None` and writes nothing for a goal whose `status != 'active'` --
proposed, paused, done and dropped goals are never checked or counted. The rule, `RULE_VERSION = "metas-v1"`:
habits for the person (assigned to them or to everyone) are "due" every day of the week, narrowed to the days a
`context` habit's shift condition holds (`before_night`/`after_night`/`off_days`, read from `shift`; no matching
shift day this week -> that habit contributes 0 due and 0 done, "not due" as the spec says); a `metric` goal needs
`MIN_METRIC_DAYS = 4` days of data this week or `on_track`/`metric_avg` are both `NULL`; `direction='keep'` compares
the week's average against the person's own p25-p75 band (`baselines.baseline`, evaluated as of the week's Monday);
`raise`/`lower` compare it against the straight line from `baseline_value` at `start_day` to `target_value` at
`deadline`, evaluated at the week's Sunday (clamped to the line's two ends; `on_track=NULL` if the goal carries no
deadline or no baseline/target, an honest gap); a `milestone` goal's `on_track` comes from habits alone, >=
`MILESTONE_ON_TRACK_PCT = 0.70` of due habits done when any are due, else `NULL`. `habits_done`/`habits_due` are
always computed and stored, even for a metric goal, for display.

**`body/vida_export.py`**: `goals` now reads `goal_check` (was `weekly_check`), joined on the person's own
`person_id` as well as `week_start`, for the person's own `status='active'` goals -- same shape as before
(`{goal_id, on_track}`). New `goal_checks` list, per vida-v1 (`goal_id, person, week_start, metric_avg, on_track,
note`): for the exporting person's household (via `household.household_of`), their own `goal_check` rows on that
household's `status='active'` shared goals (`goal.hogar_id` matching). Body has no per-member vida person-key for
anyone but the exporting installation's owner (no Cuenta sign-in yet, per `body/household.py`'s own comment), so
`goal_checks` carries the file's existing local placeholder, `PERSON_KEY = "carlos"`, same as the rest of this
export -- it is this installation's own check on a shared goal, not a merge of every member's.

**Tier.** `body/goals.py` stays Tier 2 as decided: it owns no schema and sends nothing anywhere. Re-read
`CLAUDE.md`'s Tier 1 paragraph for a table name this slice makes false -- none; it names modules, not table names,
so it is left unchanged.

**Tests** (`tests/test_goals.py`, new, 18 tests; `tests/test_vida_export.py`, +2 tests): lifecycle
(proposed->active->paused->done; drop); a proposed goal is never checked (`weekly_check` returns `None`, writes no
row) and never exported; metric on-track/off-track for `raise` and `lower`; `keep` inside and outside the band;
fewer than 4 days -> `NULL`; milestone from habits, on and off track; a `before_night` habit counted only on the day
before a recorded night shift, and not due at all with no shifts recorded; a shared household goal split into two
members' own `goal_check` rows. Migration test: the exact old shape quoted in `BODY-METAS-OBS-1` built from scratch,
rows inserted, `db.init()` run, every row's survival asserted under the new shape with mapped cadence JSON and
`rule_version='legacy'`, a second `init()` changing nothing, and a monkeypatched `_copy_habit_row` forcing a count
mismatch that rolls back leaving `plan_item` intact and `habit` never created. `tests/test_vida_export.py` gained
`goals` filtering out a proposed goal and a `goal_checks` case for a shared household goal.
`tests/test_household.py` needed no change: `hogar_id` stayed a column of `goal`, never moved.

No cross-repo contract change; no customer-facing bytes (no screens -- `BODY-METAS-SCREENS-1` follows). **Schema did
change** (`goal` rebuilt, `plan_item`/`weekly_check` replaced by `habit`/`goal_check`, `habit_check`/`plan` added),
which trips merge policy A's schema condition. The spec's own GATE section says to stop before merging on a schema
change, so this slice stops on `feat/body-metas-1` AWAITING_APPROVAL rather than merging.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 91 tests, OK (71 before this slice, 20 added
here), run once on the final branch tree before this entry.

## BODY-METAS-1-M -- land BODY-METAS-1 (2026-11-06)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after reviewing the diff (6 files, +928/-38)
against `main`: `db.py` shares one column definition between `SCHEMA` and the rebuilds; `init()` runs
`_migrate_metas` before `executescript(SCHEMA)`, so an existing `plan_item`/`weekly_check` is rebuilt before
`CREATE IF NOT EXISTS` could shadow it; each rebuild runs in `_rebuild_with_fk_off` (foreign keys off, `BEGIN`,
copy, count compare, `PRAGMA foreign_key_check`, rollback and raise on any failure, foreign keys restored), the
`goal` rebuild guarded by its CHECK text lacking `'proposed'`; legacy cadence mapped with the text kept in
`cadence_text_legacy`; `goals.py` and `vida_export` as specced. The real database was backed up by the
orchestrator before merge (`~/.body-app/body.db.bak-pre-metas-2026-11-06`). Merged unchanged -- no code change in
this slice.

`main` had not moved since the branch was cut: `git merge-base feat/body-metas-1 main` and `git rev-parse main`
both read `14f3ea6e895dd15d1dcacefe3804ce5a88ae2ad1`, matching the branch's recorded base, so no merge-forward was
needed.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 91 tests, OK, run once on the branch tip
(`e203c83c5a8fe97c01b64c6e51e7d39b4f801a76`) before this merge.

Merge commit: recorded in the next entry's git history (this entry precedes the `--no-ff` merge, per
`BODY-TURNOS-IMPORT-1-M`'s precedent).

## BODY-METAS-SCREENS-1 -- the Metas screens, built to design round 1 (2026-11-06)

Branched from `main` (`c3fd89bc1b60a4dbea3cc6bd69a4d4c016452f33`). Built on `BODY-METAS-1`'s `body/goals.py` and
schema; `body/goals.py` itself is untouched (called, not modified), and no Tier 1 path (`body/db.py`,
`body/garmin_adapter.py`, `body/login.py`, `body/config.py`, `body/vida_export.py`, `tests/_isolate.py`,
`body/listen.py`, `body/shift_import.py`) was touched.

**Routes, in `body/web.py`.** `GET /goals` (`goals.html`): active goals as cards (chip verdict, this week's
figure, target/deadline sentence, a progress bar, "Empezaste en ... De tus datos" for metric goals), a
«Propuestas» section with one-tap «Activar»/«Descartar» forms, and a collapsed «Archivo» (`<details>`, as
`shifts.html`'s per-file list) for paused/done/dropped goals; an honest empty card when there are no goals at
all. `GET/POST /goals/new` (`goal_new.html`): kind (medida/hito), title, why, area, and for a metric goal the
metric (from `metric_def`), direction (defaulted client-side from the metric's `better`, editable), target,
baseline (prefilled from `baselines.baseline()`, "De tus datos"), deadline; for a milestone, just the date;
visibility private/household only offered when `household.household_of()` finds one. Creates the goal with
`goals.propose(..., status='proposed')` and redirects to its detail. `GET /goals/{id}` (`goal.html`): the
metric goal's line from baseline to target by deadline (a small inline SVG, see below), the last 8 weeks'
`goal_check` rows in words, its habits with this week's done/due and a check-off box on the days they're due,
and pause/finish/drop (or activate/discard for a still-proposed goal). A household goal also lists every other
member's own latest `goal_check` verdict, read from the same local db (today's model, per `household.py`'s own
comment: no cross-install sync yet). `POST /goals/{id}/activate|pause|finish|drop`, `POST /goals/{id}/habits`
(adds a habit: title, cadence per/times, optional shift context), `POST /habits/{id}/check` (day + done,
redirects to a `back` path) -- the one check-off endpoint serves both Hoy's plan card and the goal detail, per
the spec's surface line. `GET /` (`today()`) gained a plan card: today's due habits across every active goal,
each a one-tap check box; renders nothing when none are due.

**Shift-context due days are never re-derived here.** `_due_days_for`/`_is_due_today`/`_habit_progress` in
`body/web.py` call `goals._context_due_days`/`goals._week_days` directly (private, but the rule they implement
is the sensitive part the spec said to call, not re-implement); only the per-habit and per-day grouping around
them is new, local display code.

**Where this departs from the mock, and why.** (1) The mock's Metas screen shows one goal's full "¿Está
funcionando?" sentence and habit breakdown inline on the list. The spec's own per-screen split puts that
review on the detail page instead (item 2), one tap from the list card's chip -- DESIGN.md rule 2, "answer
first, context one step away, never deleted". (2) The mock drew only metric-goal examples; milestone goals
(not mocked) get: list card figure "{done} de {due} esta semana" and a progress bar reading time elapsed from
start to deadline (no metric line to plot); detail page shows the deadline and habits only, as the spec asked,
with no chart. (3) The goal-detail chart (dashed target line from baseline to target, the actual weekly
averages plotted over it, today marked) is new -- `body/profile.py`'s chart helpers are built for daily Garmin
history, not a goal's own plan, and `body/profile.py` is not in this slice's `touches:`, so a small
`_goal_line_svg` was written locally in `body/web.py` instead of extending it.

**i18n.** Every new string added to both `es.json` and `en.json` under matching keys (checked programmatically:
no key exists in one file and not the other). Verdict words: "En camino" / "Sin cambio aún" / "Aún sin datos
suficientes" (es), "On track" / "Off track" / "Not enough data yet" (en), as the spec asked.

**Tests** (`tests/test_goals_web.py`, new, 8 tests, `tests/_isolate` imported first): the empty state in both
languages; `/goals` with an active, a proposed and an archived (paused) goal all present together; creating a
metric goal and a milestone goal through `POST /goals/new`, landing on each one's detail still `proposed` with
an «Activar» button; activating a goal, adding a habit, checking it off, and seeing that check land in
`habit_check`; a `before_night` habit shown on Hoy on the day before an invented night shift and hidden on a
day with no matching shift; goal detail rendering for both kinds; a household goal's other member's check
rendered by name. No health figure appears in any redirect `Location` (checked by regex on `/goals/\d+` and
exact `/` matches) or anywhere in a test fixture (all invented).

A real defect was caught and fixed while writing these tests: the test module's helper repointed
`config.DB_PATH` outside `tests._isolate`'s `TMP` directory and never restored it, so a later-loaded module in
the same `unittest discover` run (`test_pipeline.py`, alphabetically after `test_goals_web.py`) inherited the
wrong path and failed its own "still under TMP" guard. Fixed by nesting the new path under `TMP` and restoring
the original via `addCleanup` after each test -- the same pattern `test_shift_import.py` already uses for this
exact hazard.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 105 tests, OK (97 before this slice, 8
added here), run once on the final branch tree before this entry.

**Merge.** This diff is customer-facing bytes from end to end (every screen, every string a person reads), so
it trips merge policy A's customer-bytes condition regardless of the gate's result. Per this spec's own GATE
section, it stops here, on `feat/body-metas-screens-1`, clean, as `AWAITING_APPROVAL` rather than merging.

Layout points for the orchestrator's 375 px pass: the goal detail's pause/finish/discard row is three
`.confirm-actions` buttons across one flex row, which may be tight at phone width; the habit check-off control
(`.boxbtn`) is sized 28 px wide by a 44 px tall tap target around a visually smaller 20 px box, to keep the
mock's look while meeting the 44 px rule; the new goal-line SVG has no on-screen legend/caption, unlike
`trends.html`'s charts; metric goal creation takes target/baseline as a raw number in the metric's own unit
(e.g. minutes for sleep), not a friendlier time picker.

## BODY-METAS-SCREENS-1-M -- merging BODY-METAS-SCREENS-1 unchanged (2026-11-06)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after reviewing the diff; merged unchanged.
No code changed in this slice: only this ledger entry was added before the merge.

Branch `feat/body-metas-screens-1` was at `c06b719b550a50c82d1ca92a1ab886e33541f271` (tree
`003ce5d051137daac597ce119d1fd290e8abd5ae`), cut from `main` at `c3fd89bc1b60a4dbea3cc6bd69a4d4c016452f33`.
`main` had not moved since -- its tip was still `c3fd89bc1b60a4dbea3cc6bd69a4d4c016452f33`, equal to the
branch's merge-base with it -- so nothing was merged forward into the branch first.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- run once on the final tree (this entry's
own commit, after the append above) -- 105 tests, OK.

Merge commit: recorded in git history as the `--no-ff` merge of `feat/body-metas-screens-1` into `main`
following this entry.

## BODY-WEB-PYTHONW-1 -- the web app no longer crashes under pythonw (2026-11-06)

The "Body app web" scheduled task from `BODY-WEB-AUTOSTART-1` runs `pythonw.exe` so no console window appears at
logon, but `pythonw.exe` gives the process no console at all: `sys.stdout` and `sys.stderr` are `None`. The
orchestrator measured this directly -- the registered task ran and exited with `LastTaskResult 1`, nothing
listened on port 8770, the same command run with `python.exe` served normally, and simulating `pythonw` (setting
both streams to `None` before running `body.web` as `__main__`) reproduced `logging/config.py ...  ValueError:
Unable to configure formatter 'default'` -- uvicorn's default log config builds stream handlers on the missing
streams.

**Fix, in `body/web.py`.** A new `_ensure_streams()`: if `sys.stdout` is `None`, replace it with
`open(os.devnull, "w")`; same for `sys.stderr`. `main()` calls it first, before anything prints or uvicorn is
imported. No log file is written -- health data never goes to a log, so the access log stays off
(`access_log=False`, `log_level="warning"`, both unchanged, as the spec required).

`tests/test_web_entry.py` (new, 3 tests, `tests/_isolate` imported first): `_ensure_streams()` with both streams
set to `None` (restored in `finally`) leaves both non-`None` and writable; with both present it leaves the exact
same objects in place (`assertIs`); `main()` with `sys.stdout`/`sys.stderr` both `None` and `uvicorn.run` patched
reaches `uvicorn.run` without raising, called with `access_log=False`.

No schema or migration change; no cross-repo contract change; no customer-facing bytes (nothing a browser ever
renders changed -- this is a startup-only fix for a server that has no console to show anything on). Merges on
green per merge policy A.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 108 tests, OK (105 before this slice, 3
added here), run once on the final branch tree before this entry.

## BODY-METAS-SCREENS-POLISH-1 -- two fixes from the 375 px measurement of the Metas screens (2026-11-06)

Branched from `main`. Fixes the two gaps the orchestrator's 375 px pass of `BODY-METAS-SCREENS-1` recorded
against `docs/DESIGN.md` rule 11 (phone first, 44 px targets): on `/goals/new`, 9 interactive elements rendered
under 36 px tall, and the form showed the metric-only fields (metric, direction, target, starting point) even
when «Hito» / "Milestone" was chosen.

**44 px, in `body/static/body.css`.** `.field`'s and `.btn`'s `min-height` raised from 40px to 44px -- both are
`min-height`, not a fixed height, so taller content (the `why` textarea, multi-line buttons) still grows. Every
control in the touched templates reaches `.field`/`.btn`/`.boxbtn` through those shared classes with no inline
height override; re-reading each of `goal_new.html`, `goal.html`, `goals.html`, `today.html` and `shifts.html`
found none. `goal.html`'s "Agregar hábito" `<details>` (the one disclosure trigger in the touched templates not
already carrying `.files`) gained that class, so its `<summary>` picks up the existing `details.files summary`
44 px rule the same way `shifts.html`'s per-file list and `goals.html`'s archive already do.

**Kind-dependent fields without JS, in `goal_new.html`.** The goal-kind `<select>` became two radio inputs,
`name="kind"` `value="metric"`/`"milestone"` (metric checked by default, same as the select's first option),
each wrapped in a `label.radio` (new CSS: `display:flex`, `gap:8px`, `min-height:44px`, with the native input
sized to a visible 20px box) -- the 44 px checkbox/radio label rule from the same spec line. The metric-only
block (metric, direction, target, baseline) moved into a `<fieldset class="metric-only">` around its existing
`.card`; a new global `fieldset{border:0;margin:0;padding:0}` keeps it invisible as a box so the `.card` inside
still looks like every other card. Hidden with `form:has(input[name=kind][value=milestone]:checked)
.metric-only{display:none}` -- no JavaScript, as the spec asked; supported by the phone's Chrome. The date
field keeps one input for both kinds but swaps its label: `.q.metric-only` ("Fecha meta" / "Goal date", new key
`goals.field_deadline_metric` in both `es.json` and `en.json`) and `.q.milestone-only` (the existing
`goals.field_deadline`, "Fecha" / "Date"; `.milestone-only` is hidden by default and shown by the same `:has()`
rule). `body/web.py`'s `/goals/new` POST handler reads `kind`/`metric_key`/`direction`/`target_value`/
`baseline_value`/`deadline` from form fields by name regardless of control type, so the server-side validation
for each kind is unchanged, per the spec. The `bodyGoalKind()` script (the old select's onchange) was removed
along with the select; `bodyMetricChange()` (direction/baseline autofill from the chosen metric) is unrelated
to the kind switch and stays.

**Tests**, in `tests/test_goals_web.py` (2 new): `/goals/new`'s rendered HTML carries `metric-only` and both
`name="kind" value="metric"`/`"milestone"` radio inputs; `body/static/body.css`, read as text, contains the
`.btn` and `.field` rules with `min-height:44px`.

No schema or migration change; no cross-repo contract change. This diff is customer-facing bytes end to end --
new radio markup and a new label a person taps and reads, a new i18n key, a resized tap target on every button
and input in the app -- so it trips merge policy A's customer-bytes condition regardless of the gate's result.
Per this spec's own GATE section, it stops here, on `feat/body-metas-screens-polish-1`, clean, as
`AWAITING_APPROVAL` rather than merging.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 110 tests, OK (108 before this slice, 2
added here), run once on the final branch tree before this entry.

## BODY-METAS-SCREENS-POLISH-1-M -- landing BODY-METAS-SCREENS-POLISH-1 unchanged (2026-11-06)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after reviewing the diff; merged unchanged.
No code changed in this slice: only this ledger entry was added before the merge.

Branch `feat/body-metas-screens-polish-1` was at `bf56f683686b707cbde038c46290a8ad599d19ed`, cut from `main` at
`a0f12978707d37b40afd5c5b83a935c8d903eb76`. `main` had not moved since -- its tip was still
`a0f12978707d37b40afd5c5b83a935c8d903eb76`, equal to the branch's merge-base with it -- so nothing was merged
forward into the branch first.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- run once on the final tree (this entry's
own commit, after the append above) -- 110 tests, OK.

Merge commit: recorded in git history as the `--no-ff` merge of `feat/body-metas-screens-polish-1` into `main`
following this entry.

## BODY-STATIC-VERSION-1 -- a CSS/JS change now reaches a phone that already cached the old one (2026-11-06)

Measured by the orchestrator: in the Browser pane, `/goals/new` kept drawing the OLD stylesheet
(`/static/body.css?v=3`, 8,321 bytes, no new rules) while the file on disk was 10,238 bytes with them, because
the service worker (`body-static-v2`) answers every `/static/` request from its own cache first and
`base.html` always linked the same hand-bumped `body.css?v=3`. Carlos's phone carries the same worker, so this
was not only a browser-pane artifact.

Fix, in `body/web.py`: `_static_hash(directory)` hashes (sha256, first 10 hex chars) every file under a
directory by sorted relative path plus bytes; `STATIC_HASH = _static_hash(HERE / "static")` is computed once
at import, and `static_url(path)` returns `/static/<path>?v=<STATIC_HASH>`, registered as a Jinja global
(`env.globals["static_url"]`) rather than threaded through every `_render()` call, since -- unlike `t()`,
which is per-request (the person's chosen language) and passed as a kwarg each render -- it carries no
per-request state. The service worker's cache name is now `body-static-<STATIC_HASH>` (built once at import by
substituting into `SW_TEMPLATE`, not an f-string, to avoid escaping the worker's own JS braces); its existing
`activate` handler already deletes every cache key that isn't the current one, so a new build's hash evicts
the old cache automatically. `/sw.js` now answers with `Cache-Control: no-cache` so the browser never caches
the worker script itself; it still caches only `/static/` and never a health page.

`body/templates/base.html`: all three `/static/` references (`body.css`, `manifest.webmanifest`, `icon.svg`)
now go through `{{ static_url(...) }}`; grepped every template under `body/templates/` for `/static/` first --
`base.html` was the only one with any (the other templates all `{% extends "base.html" %}` and carry no
static references of their own).

**Tests**, `tests/test_static_version.py` (6 new): `/` (via a fresh empty db, which renders `soon.html`,
itself extending `base.html`) links `body.css` with `?v=` equal to `web.STATIC_HASH`; `web.static_url("body.css")`
returns the same URL; no file under `body/templates/` contains a literal `?v=`; `/sw.js` carries
`body-static-<STATIC_HASH>` in its text and answers `Cache-Control: no-cache`; `web._static_hash()` called on a
temp directory changes when a file's bytes change and is stable across two directories with identical
content -- no real static file is edited by the tests.

No schema or migration change; no cross-repo contract change. No customer-facing bytes either: the rendered
CSS, icon and manifest are byte-for-byte what they always were, and the service worker's cache-key scheme is
plumbing a person never reads -- only the cache-busting query string and the worker's internal cache name
change, never anything visible on a screen. All three of merge policy A's conditions pass, so this merges on
green without stopping for approval.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 116 tests, OK (110 before this slice, 6
added here), run once on the final branch tree before this entry.

## BODY-ASISTENTE-REGISTRO-1 -- stage one of the assistant: nivel_datos and a static lectura tool registry (2026-11-06)

Carries out BODY-ASISTENTE-OBS-1 (OBSERVED, 2026-11-06), slice (a) of its plan: the data half of wave 7's
assistant, with no model call, no provider client and no screen. Two pieces.

**`ajuste_agente`** (`body/db.py`'s `SCHEMA`, one `CREATE TABLE IF NOT EXISTS`, additive, no rebuild, no
existing row touched): `person_id INTEGER PRIMARY KEY REFERENCES person(id)`, `nivel_datos TEXT NOT NULL
DEFAULT 'completo' CHECK(nivel_datos IN ('completo','resumen','ninguno'))`, `updated_at TEXT NOT NULL`. The
three levels and the default are Carlos's own answer, recorded in the Body orchestrator's ledger as
BODY-ASSISTANT-DATA-LEVEL-1 ("Assistant: 1."). `body/agente/ajustes.py` reads it (`nivel_datos`, row or
`"completo"` with no row) and writes it (`set_nivel_datos`, validated against the `NIVELES` tuple, upserted,
`updated_at` stamped) -- the only writer this slice adds.

**The registry**, `body/agente/herramientas.py`, `HERRAMIENTAS`: nine entries, every one `tipo: "lectura"`,
each `nombre` -> minimum `nivel`:

| nombre | nivel | screen |
|---|---|---|
| `leer_hoy` | completo | `/` |
| `leer_dia` | completo | `/day/{day}` |
| `leer_como_calculo` | completo | `/how/{day}` |
| `leer_tendencias` | resumen | `/trends` |
| `leer_historia` | resumen | `/history` |
| `leer_metas` | resumen | `/goals` |
| `leer_meta` | resumen | `/goals/{goal_id}` |
| `leer_turnos` | ninguno | `/shifts` |
| `leer_ajustes` | ninguno | `/settings` |

Each `funcion(conn, person_id, **params)` calls an existing read function (`readiness.compute`,
`db.day_values`, `db.load_day`, `db.person`, `db.days_with_data`, `profile.metric_profile`, `profile.window`,
`history.coverage/training/patterns/by_shift`, `shift_import.summary_proposed/latest_confirmed`) or a plain
`SELECT`; none calls `body/web.py`, so the module stays free of FastAPI/Jinja. `leer_metas` and `leer_meta`
read the newest EXISTING `goal_check` row with a direct `SELECT ... ORDER BY week_start DESC LIMIT`, saying
"sin verificación aún" when there is none, rather than calling `_goal_check_row` / `goals.weekly_check` --
`/goals` and `/goals/{goal_id}` upsert that row on a normal visit, and a `lectura` tool must never write.
`leer_tendencias`'s aggregates (means, p25/p75, counts) never include `profile.Window.values` (the per-day
dict `window()` also returns), so a `resumen`-level answer never carries a single day's figure.

`disponibles(nivel)` filters the registry by rank (`ninguno` < `resumen` < `completo`): `completo` returns all
nine, `resumen` the six at `resumen`/`ninguno`, `ninguno` the two at `ninguno`. `ejecutar(conn, person_id,
nombre, params, nivel)` raises `ToolError` for an unknown name, a tool above the caller's `nivel`, or
parameters that fail the entry's small JSON-schema `parametros` dict (required keys, enum, type, and an ISO
date format check) -- otherwise it calls the tool and returns its (JSON-serialisable) dict.

**Tests**, `tests/test_agente_registro.py` (`_isolate` first, invented demo data only; 20 new): a `ROUTE_TOOL`
mapping from every GET route in `body/web.py` (`app.routes`, `APIRoute`, `"GET" in r.methods`) to its tool or
to `None` with a reason (`/goals/new` is a creation form, `/sw.js` a static asset) -- the test asserts the
route set equals this mapping's keys exactly, so an added or removed GET route fails until the mapping is
updated by hand. A STATIC guard (`ast` + `inspect`, bounded at depth 8, a `seen` set against cycles) walks
each tool's source and the `body/`-module functions it calls, transitively, flagging a `conn.execute`/
`executemany` whose first-argument string literal starts with INSERT/UPDATE/DELETE/REPLACE, or a call to any
of the thirteen known writer names (`weekly_check`, `propose`, `activate`, `pause`, `finish`, `drop`,
`add_habit`, `check_habit`, `confirm`, `confirm_all`, `dismiss`, `set_language`, `set_nivel_datos`) -- none of
the nine reaches one. A BEHAVIOURAL guard runs every tool on a seeded database and asserts `conn.total_changes`
does not move. `disponibles` is checked against the exact three level sets above; `ejecutar` is checked to
refuse `leer_dia` at `resumen` and `ninguno`, an unknown tool name, and a call missing `leer_dia`'s required
`day`. `leer_tendencias` and `leer_historia` are checked to carry no per-day value. `leer_metas`/`leer_meta`
are checked to say "sin verificación aún" (and to leave `goal_check` at zero rows) when none exists yet, and
`leer_meta` to report `encontrada: False` for an unknown `goal_id`. `nivel_datos` is checked for its
`"completo"` default with no row, a set/get round trip, and refusal of an invalid level both by
`set_nivel_datos` (`ValueError`) and by the table's own `CHECK` (`sqlite3.IntegrityError`). Every tool's
output is checked to survive `json.dumps`.

**Tier 1 path added**: `CLAUDE.md`'s Tier 1 sentence now also names `body/agente/cliente.py` ("the client that
will send anything to a model provider ... the only module that may do so, once it exists"), so the slice
that writes it is gated before it exists.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 136 tests, OK (116 before this slice, 20
added here), run once on this slice's final tree.

This slice's diff adds one `CREATE TABLE IF NOT EXISTS` to `body/db.py`'s `SCHEMA` -- a schema change, merge
policy A's first condition. No customer-facing bytes (no template, no screen, no string a person reads) and
no cross-repo contract change, but failing one condition is enough: per this slice's own spec ("Schema
change: stop on the branch clean and report AWAITING_APPROVAL (merge policy A) unless the policy says
otherwise") this stops on the branch, committed, unmerged, for approval.

## BODY-ASISTENTE-REGISTRO-1-M -- landing BODY-ASISTENTE-REGISTRO-1 unchanged (2026-11-06)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after reviewing the diff; merged unchanged.
No code changed in this slice: only this ledger entry was added before the merge.

Branch `feat/body-asistente-registro-1` was at `3a01e83621ae085227a140bc0d6131cb3ec19cb9`, cut from `main` at
`62e0770901c97bab68d0ac7d3e7d866c7eaa08b0`. `main` had not moved since -- its tip was still
`62e0770901c97bab68d0ac7d3e7d866c7eaa08b0`, equal to the branch's merge-base with it -- so nothing was merged
forward into the branch first.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- run once on the final tree (this entry's
own commit, after the append above) -- 136 tests, OK.

Merge commit: recorded in git history as the `--no-ff` merge of `feat/body-asistente-registro-1` into `main`
following this entry.

## BODY-SIN-FRICCION-1 -- Lotes' "sin fricción" principles, adopted into Body (2026-11-06)

Transferable principles from Lotes' "sin fricción" rounds (hub `hit_01m6p3jv5fnw4pfd5wsckxr0j5`,
`hit_01m6pm4aagg2hgz8b36ehbw7cf`; Body replied `revisado` 2026-11-06): an empty state never dead-ends, a picker
with an empty catalogue keeps its label and a create option, and a new goal can get its first habit in the
same flow.

**Census of every empty state** (grepped `empty`, `no_data`, `_empty` across `body/templates/`; `soon.html`
call sites in `body/web.py`):

| screen / state | before | after |
|---|---|---|
| Hoy, no day ever synced | `today.empty`: "Ejecuta {cmd}" (a CLI instruction, not a web fix) | `sync.runs` + (`sync.last` with the last successful sync's date, from `db.last_sync`, or `sync.none_yet`) |
| Día, no data for that day | `day.no_data` alone, no explanation | `day.no_data` + the same sync explanation (`sync_text`) |
| Tendencias, zero days ever synced | fell to `soon.trends` (a description of the trends charts, not an explanation) | the same sync explanation as Hoy/Día |
| Tendencias, 1-13 days (no metric reaches `profile.metric_profile`'s 14-day floor) | silently fell to the same `soon.trends` text, with no count | `trends.too_little`: the day count still needed (`TREND_MIN_DAYS - have`, a count, never a figure) |
| Tu historia, zero days ever synced | fell to `soon.trends` (wrong title's worth of text: "Tu historia" page, trends-chart copy) | the same sync explanation |
| Tu historia, patterns/shift empty (`history.patterns_none`, `history.shift_empty`) | already links `/shifts` | kept unchanged |
| Turnos, nothing proposed or confirmed (`soon.shifts`) | already says shift files arrive via the Personal import | kept unchanged |
| Metas, no goals (`goals.empty_text`) | already offers "+ Nueva" | kept unchanged |

`today.empty` (and its unused `cmd`/`garmin_adapter.has_token()` plumbing) is removed; nothing else referenced
it. The new `_sync_status_text(conn, person, t)` helper (`body/web.py`) is shared by all four zero-data states
above. It never invents a schedule time -- the actual time is a per-install Windows Task Scheduler setting
(`scripts/install-daily-sync.ps1`'s `-At`) this app cannot read -- it only says the sync runs on its own, and
names the last successful sync's date when `db.last_sync` has one.

**Picker with an empty catalogue** (`body/templates/goal_new.html`): the Metric row, when `metrics` is empty,
keeps its `goals.field_metric` label and shows `goals.metric_empty` ("metrics appear after your first sync")
instead of an empty `<select>`. In today's code this is defensive only -- `db.init()` reseeds `metric_def` on
every request, so the catalogue can't actually run dry -- exercised in the test by patching `web._defs` to
return `{}`. The Visibility row, when there is no household, now always renders its `goals.field_visibility`
label; without a household it shows `goals.household_empty` instead of vanishing (no new creation flow:
Cuenta owns households, per `hogar-v1`). This IS today's live default state for a single-person install, so
the test needs no mocking. `bodyMetricChange()`'s JS guards `document.getElementById('metric-select')` being
null now that the `<select>` can be absent.

**First habit in the same flow** (`body/templates/goal_new.html`, `body/web.py`): the "+ Nueva" form gets an
optional habit block (`habit_title`/`habit_times`/`habit_per`/`habit_context`, prefixed so they never collide
with the goal's own `title` field) inside a `<details>`, the same shape `goal.html`'s "add a habit" form
already uses. `goal_new_create` parses it with the new `_parse_new_habit(f, prefix)` helper (factored out of
`goal_add_habit`, which now calls the same helper with no prefix) and, when filled, calls `goals.add_habit` in
the same request before redirecting to `/goals/{gid}`, so the new goal's detail page lists the habit already.
An empty block creates no habit -- unchanged behaviour.

**Not applicable in Body** (recorded, not built, per the spec): photo/receipt slots, prices, −/+ counters,
sales and commissions -- Body has none of these.

**Tests**, `tests/test_sin_friccion.py` (`_isolate` first, invented demo data; 14 new): one test class per
census row above plus the two pickers and the habit-creation flow, each checked in Spanish and English;
`NoFigureInRedirectTest` checks a goal-creation redirect (with a habit attached) carries only `/goals/{id}`,
no query string.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 150 tests, OK (136 before this slice, 14
added here), run once on this slice's final tree.

This slice's diff changes customer-facing text and markup in `body/templates/day.html`,
`body/templates/goal_new.html`, `body/web.py` and both `body/i18n/*.json` files -- no schema or migration
change, no cross-repo contract change, but
merge policy A's "customer-facing bytes" condition trips on its own. Per this slice's own spec ("Customer-
facing bytes: stop on the branch clean and report AWAITING_APPROVAL unless merge policy says otherwise"),
this stops on the branch, committed, unmerged, for approval.

## BODY-SIN-FRICCION-1-M -- land BODY-SIN-FRICCION-1 (2026-11-06)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after reviewing the diff; merged unchanged.
No code changed in this slice: only this ledger entry was added before the merge. Approval reasoning: the two
deviations recorded above (trends' empty branch split into no-data vs too-few-days with a days-still-needed
count, and the history fallback's wrong copy fixed; the empty-metric picker branch kept as defensive and
tested by patching) were reviewed and accepted. Measured in a real browser at 375 px on the branch with the
service worker cleared: `/goals/new` has 16 visible controls, none under 44 px, the optional "Add a habit"
block with title/times/per/context, no horizontal scroll; `/day` for a date with no data says the sync runs
on its own and names the last sync date.

Branch `feat/body-sin-friccion-1` was at `3f490f925470374d7b3e05272eb2d62b3ce8641f`, cut from `main` at
`03e476e20869a2e2efe8e663919e34de25c72f41`. `main` had not moved since -- its tip was still
`03e476e20869a2e2efe8e663919e34de25c72f41`, equal to the branch's merge-base with it -- so nothing was merged
forward into the branch first.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- run once on the final tree (this entry's
own commit, after the append above).

Merge commit: recorded in git history as the `--no-ff` merge of `feat/body-sin-friccion-1` into `main`
following this entry.

## BODY-PERSON-SCOPE-1 -- Body stops assuming one person (2026-11-06)

Carries out BODY-HOST-OBS-1 (OBSERVED, 2026-11-06), which measured identity as unscoped at the web layer only:
`body/db.py` defined `DEFAULT_PERSON_ID = 1` and 52 sites across `body/web.py` and the data modules defaulted
`person_id` to it, even though every lower module already took `person_id` as a parameter. Required by
host-deploy-v1-amendment-sensitive-data rule 4 (every read filtered by the signed-in person, B gets 404 on A's
row). No schema change and no data change: the same rows, read through an explicit person instead of a
default.

**Census**: `grep -rn DEFAULT_PERSON_ID body/ tests/` before this slice found 48 occurrences across 11 files --
9 keyword-defaulted parameters in `body/db.py` itself (`save_day`, `load_day`, `set_metric`, `series`,
`person`, `set_language`, `day_values`, `last_sync`, `days_with_data`), one default each in
`body/baselines.py` (`baseline`), `body/profile.py` (`metric_profile`, `window` -- 2), `body/readiness.py`
(`compute`, `update`, `recompute_all` -- 3), `body/history.py` (`coverage`, `training`, `patterns`,
`by_shift` -- 4), `body/household.py` (`household_of`), `body/vida_export.py` (`week_section`),
`body/agente/ajustes.py` (`nivel_datos`), `body/shift_import.py` (`propose`, `confirm_all`,
`summary_proposed`, `latest_proposed`, `latest_confirmed`, and the CLI's `--person-id` default -- 6), 15
literal uses of `db.DEFAULT_PERSON_ID` in `body/web.py`'s route bodies and SQL parameters, and 3 in
`tests/test_history.py`'s seed helper. After this slice, `grep -rn DEFAULT_PERSON_ID body/` finds 3
occurrences, all in `body/db.py` (the constant's definition, `init()`'s one-time seed insert, and the new
`local_person_id()`); no other module under `body/` mentions it.

**The resolver, `body/identity.py`**: `current_person_id(request: Request) -> int`, wired into every route as
a FastAPI dependency (`person_id: int = Depends(identity.current_person_id)`), not called directly, so a test
can swap it with `app.dependency_overrides[identity.current_person_id] = lambda: other_id` without touching
any route. Today, with no Cuenta sign-in, it resolves to `db.local_person_id(conn)`, a new one-line function
that returns `DEFAULT_PERSON_ID` -- the one row `init()` ensures exists. This is the single seam wave 8's
Cuenta sign-in slice will replace.

**`person_id` is now required** everywhere the census found a default, in every function listed above: the
keyword (or positional, matching each function's existing style) was kept, only the `= db.DEFAULT_PERSON_ID`
default dropped. `body/goals.py` and `body/agente/herramientas.py` were already fully explicit (in `touches`
but needed no edit). CLI entry points resolve the local person once at the top and pass it down:
`body/pull.py`'s `main()` calls `db.local_person_id(conn)` once and threads it through `pull_all`/`_stored`/
`readiness.recompute_all`/`db.save_day`; `body/shift_import.py`'s `main()` keeps its `--person-id` flag but
defaults it to `None` and resolves `db.local_person_id(conn)` when absent; `body/vida_export.py`'s `main()`
does the same before calling `week_section`.

**`body/web.py`**: `_ctx(person_id: int)` (was `_ctx()`) opens the connection and loads the person row by that
id; every route gets `person_id` through `Depends(identity.current_person_id)` and passes it to `_ctx` and to
every person-scoped call below it (`_delta`, `_latest_day`, `_trend_days_have`, `_nights`,
`db.day_values`/`load_day`/`last_sync`/`set_language`/`days_with_data`, `baseline`, `readiness.compute`,
`profile.metric_profile`/`window`, `history.coverage`/`training`/`patterns`/`by_shift`,
`household.household_of`, `shift_import.*`). `_synced`/`_sync_status_text` read `person["id"]` from the
already-resolved person dict rather than taking a second parameter.

**404 on another person's row, never a silent no-op**: `GET /goals/{goal_id}` already 404d; `POST
/goals/{goal_id}/activate|pause|finish|drop|habits` and `POST /habits/{habit_id}/check` used to silently skip
the write and redirect back to a page that then 404d (for the goal routes) or to `back` with no write at all
(for the habit check) -- both now raise `HTTPException(404)` directly, via two small helpers
(`_owned_goal_or_404`, used by the five goal routes; the habit-check route's existing ownership `SELECT` now
raises instead of silently skipping). `shift_import.confirm`/`dismiss` take only a `batch_id` and trusted the
batch row's own `person_id` with no cross-check at the route -- `POST /shifts/confirm` and `POST
/shifts/dismiss` now check `_owned_batch_or_404` (a new helper) before calling either, so a batch id from
another person 404s instead of being confirmed or dismissed. `household.py`'s rule is unchanged: a household
goal's other members are read only through `household_of`, never another member's private health figures.
Day-based routes (`/day/{day}`, `/how/{day}`) take no id of another person's row; they already filter by the
resolved `person_id` and show an honest empty state for a day with no data for that person, which needed no
404.

**No behaviour change in local mode**: every existing test stayed green with only the explicit-argument edits
each call site needed (listed below); the local person's pages render identically.

**Tests**: `tests/test_history.py`, `test_profile.py`, `test_readiness.py`, `test_vida_export.py`,
`test_goals_web.py`, `test_pipeline.py`, `test_agente_registro.py` -- every call site the census found that
relied on a now-removed default got its `person_id` (or `1`, matching how that test already seeded the local
person) made explicit; no assertion changed. `tests/test_person_scope.py` (new, 10 tests, `_isolate` first,
invented demo data, two persons A and B): seeds each with a day of data, an active goal with a daily habit
and a check, and a proposed shift batch; overrides the resolver to A and checks `/`, `/goals` and `/shifts`
render only A's goal/habit titles and batch date, not B's (and the mirror for B); checks `GET
/goals/{goal_id}` 404s on the other person's id; checks `POST
/goals/{goal_id}/activate|pause|finish|drop|habits`, `POST /habits/{habit_id}/check` and `POST
/shifts/confirm|dismiss` all 404 on the other person's id and leave that id's row unchanged afterwards; a
static check (`pathlib.Path.rglob`) that no `.py` file under `body/` except `db.py` mentions
`DEFAULT_PERSON_ID`; a static check (`inspect.signature`) that every function the census found has no default
for its `person_id` parameter.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 160 tests, OK (150 before this slice, 10
added here), run once on this slice's final tree.

This slice's diff has no schema or migration change (no `CREATE`/`ALTER` in `body/db.py`'s `SCHEMA`), no
customer-facing bytes (no template, no i18n string, no screen renders differently for the local person --
the new 404s fire only for an id that belongs to a different person, which cannot occur in today's
single-person install), and no cross-repo contract change (`vida_export.week_section`'s JSON shape is
unchanged; only its Python signature gained a required parameter). All three of merge policy A's conditions
pass, so this merges on green without a separate approval step, unlike BODY-ASISTENTE-REGISTRO-1 and
BODY-SIN-FRICCION-1 above, which each tripped one.

## BODY-REQUEST-LOG-SCRUB-1 -- a request log that cannot carry a health figure (2026-11-06)

Premise re-verified (BODY-HOST-OBS-1): uvicorn still runs with `access_log=False` and `log_level="warning"` and no
request-logging middleware existed, so a host operator had no way to see that requests were failing.

**`body/request_log.py`** (new): `RequestLogMiddleware`, a Starlette `BaseHTTPMiddleware`. On every request it logs
one line to the `body.request` logger: `"%s %s %d %dms"` (method, the matched route's path TEMPLATE from
`request.scope["route"].path`, status code, duration in ms) -- e.g. `GET /day/{day} 200 3ms`. No route matched
(a 404 on an unknown path) logs `-` as the template, e.g. `GET - 404`. The query string, request body, headers,
cookies, client IP and user agent never reach the logger, because the line is built only from the method, the
route object and the response's own status code -- none of those can hold a value from the request. An unhandled
exception raised by the route is caught in the middleware's own `try/except` around `call_next`: it logs
`"%s %s 500 %s"` (method, template, the exception's class name only -- never `str(exc)`, never a traceback) and
returns a plain `PlainTextResponse("Internal Server Error", 500)` built fresh in the middleware, so no exception
text reaches the response body either. Because `add_middleware` places this middleware between Starlette's
`ServerErrorMiddleware` (outermost) and `ExceptionMiddleware`/the router (innermost), swallowing the exception here
means nothing is re-raised to uvicorn's own traceback logger -- `ServerErrorMiddleware` never sees it.

**`body/web.py`**: `app.add_middleware(RequestLogMiddleware)` right after the `FastAPI()` app is created. `main()`
now configures the `body.request` logger once, at INFO, with a `StreamHandler(sys.stderr)` (stderr is where
`_ensure_streams()` already points pythonw's otherwise-missing stream, and where systemd's journal picks it up on
a host) and `propagate = False` so it never doubles through the root logger. `access_log=False` and
`log_level="warning"` on `uvicorn.run` are unchanged -- no log file is written anywhere.

**Tests** (`tests/test_request_log.py`, new, 5 tests, `_isolate` first, invented demo values, no real health
data): `GET /day/<day>?x=<value>` and `GET /goals/<id>?x=<value>` each log exactly one line matching
`^(GET|POST) \S+ \d{3} \d+ms$`, containing the route template (`/day/{day}`, `/goals/{goal_id}`) and neither the
date, the goal id, nor the query value; `POST /goals/new` with `target_value=73.123` logs `/goals/new` and never
`73.123`; `GET` on an unmatched path logs `GET - 404`; patching `web._delta` to raise `ValueError("73.123")` inside
`GET /day/<day>` yields a 500 response whose body does not contain `73.123`, and a log line containing `/day/{day}`,
`500` and `ValueError` but not `73.123`.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 165 tests, OK (160 before this slice, 5 added
here), run once on this slice's final tree.

This slice's diff has no schema or migration change (no edit to `body/db.py`), no customer-facing bytes (the 500
page's text, `"Internal Server Error"`, is byte-for-byte what Starlette's own `ServerErrorMiddleware` already
returned for an unhandled exception with no custom handler registered; no template, no i18n string and no 200-path
screen changed), and no cross-repo contract change (nothing here is read by another repository). All three of
merge policy A's conditions pass, so this merges on green without a separate approval step.

## BODY-CUENTA-SCHEMA-1 -- schema half of Cuenta Duna sign-in (2026-11-06)

Additive only, no existing row changes. `body/db.py`: a nullable `person.cuenta_sub` TEXT column, added by
`_migrate` when missing (same guarded-`ALTER TABLE` pattern as `language` and `batch_id` before it), plus a
partial unique index `person_cuenta_sub` on `person(cuenta_sub) WHERE cuenta_sub IS NOT NULL` (created with
`CREATE UNIQUE INDEX IF NOT EXISTS` on every `_migrate` run, so the many NULLs before anyone signs in through
Cuenta never collide). Three new tables: `sesion` (a signed-in session, keyed by `id_hash` -- a hash of the
session token, never the token itself -- with `person_id REFERENCES person(id)`, `cuenta_sid` for back-channel
logout lookups, and `created_at`/`last_seen_at`/`expires_at`, indexed on `cuenta_sid` and `person_id`);
`inicio_sesion` (one short-lived, single-use sign-in attempt, keyed by `state_hash` -- a hash of OAuth `state`,
never `state` itself -- carrying `nonce`, `code_verifier`, `return_to`, `created_at`); `consentimiento` (one
accepted consent-text version per person, `UNIQUE (person_id, version)`, `person_id REFERENCES person(id)`). No
sign-in code lands here -- BODY-CUENTA-RP-1 follows.

**Tests**: `tests/test_cuenta_schema.py` (new, 9 tests, `_isolate` first, invented demo data): a fresh install
has the column, the partial index and all three tables, and `init()` run twice is idempotent (person/metric_def
row counts unchanged); a DB built from the pre-slice schema (today's `SCHEMA` with the three new tables
dropped, since `person`'s own `CREATE TABLE` text never defined `cuenta_sub`) migrates cleanly through
`_migrate`; `cuenta_sub` uniqueness (two persons both NULL coexist; a second person cannot take an
already-used `cuenta_sub`); `consentimiento`'s `(person, version)` uniqueness; the `sesion` -> `person` foreign
key.

Gate run by the slice itself: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 174 tests, 1 failure
in `tests/test_request_log.py`
(`GetRequestLogTest.test_goal_detail_route_logs_template_not_the_id_or_query_string`), unrelated to this diff: a
pre-existing, timing-dependent weak assertion in `BODY-REQUEST-LOG-SCRUB-1`'s own test (`assertNotIn(str(gid),
msg)` checks the whole log line, which also carries the duration in ms, so it false-fails whenever that
duration's digits contain gid's digit, and `gid` is always `1` in that test's fresh db). The slice did not
merge and did not write this entry itself, stopping GATE_RED per the dev-protocol slice contract;
BODY-CUENTA-SCHEMA-1-M below records the orchestrator's review, the test fix and the merge.

## BODY-CUENTA-SCHEMA-1-M -- lands BODY-CUENTA-SCHEMA-1, fixes the request-log test's weak assertion (2026-11-06)

Approved by the orchestrator under ORCH-CARLOS-DELEGATES-TIER-1-1 after reviewing the branch: `body/db.py`'s
additions are exactly as BODY-CUENTA-SCHEMA-1 above decided, additive, no existing row changes. The one gate
failure was `tests/test_request_log.py`'s goal-detail case asserting `str(goal_id)` -- `"1"` -- absent from the
whole log line, which also contains the duration in ms, so it fails whenever the duration contains a `1`; the
orchestrator's own spec (BODY-REQUEST-LOG-SCRUB-1) wrote that weak check. The request-log test's weak assertion
fixed: `tests/test_request_log.py`'s goal-detail case now splits the logged line on whitespace and asserts the
path field equals `/goals/{goal_id}` exactly, in place of `assertNotIn(str(gid), msg)` -- stronger (still fails
if the raw id were logged as the path) and timing-free. The query-value check (`assertNotIn(SECRET_QUERY_VALUE,
msg)`) is unchanged. No other case in that file has the same weak pattern: the other asserted-absent values
(`SECRET_DAY`, `SECRET_FORM_VALUE`) are a dashed date and a decimal, neither of which can appear inside the
`\d+ms` duration field or the `\d{3}` status field.

`main` had not moved since the branch was cut: `git merge-base feat/body-cuenta-schema-1 main` and `git
rev-parse main` both resolved to `9208038d7d77175f4602c35d183e5894233f017c`, so no merge of `main` into the
branch was needed.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 174 tests, OK, run once on the branch tip
before merging.

Merge commit: recorded in git history as the `--no-ff` merge of `feat/body-cuenta-schema-1` into `main`.

## BODY-CUENTA-RP-1 -- hosted Cuenta Duna sign-in (2026-11-06)

Hosted iff `BODY_CUENTA_ISSUER` is set (then `BODY_PUBLIC_URL` and `BODY_CUENTA_CLIENT_SECRET` are required, or
the app refuses to start naming the missing variable, never a value). Local mode is unchanged: sign-in routes
404, `identity.current_person_id` resolves to `db.local_person_id`.

`body/cuenta.py` (new, Tier 1): the OIDC relying party for cuenta-duna-v1 -- authorize-URL construction with
PKCE S256, the `flujo` cookie and its single-use `inicio_sesion` row, code exchange via `client_secret_basic`,
pure ID-token/logout-token validation (`_verificar_claims`) built to pass every vector in
`tests/fixtures/cuenta_vectores.json`, sessions keyed by a sha256 of the session token (sliding 14-day idle /
30-day absolute via the `sesion` row's own `last_seen_at`/`expires_at`), sign-out and its `/oauth/logout`
redirect (never an `id_token_hint`, never a stored ID token), and back-channel logout with jti replay
protection in the durable `logout_jti` table. `acr` is compared by RANK (`pwd < passkey`), not exact string
match, per the contract's own prose -- still passes every vector, and avoids wrongly refusing a passkey
sign-in to a client whose minimum is `pwd`.

`body/identity.py`: `current_person_id` resolves the hosted-mode session from `body_sesion`, raising
`LoginRequired` (caught by `body/web.py`'s exception handler, which redirects to `/login`) for a page GET and
a plain 401 for a POST; local mode untouched.

`body/db.py` (Tier 1): `inicio_sesion` rebuilt from BODY-CUENTA-SCHEMA-1's `state_hash` shape to this
contract's `flujo_hash` shape (`flujo_hash`, `state`, `nonce`, `code_verifier`, `return_to`, `created_at`), via
the same `_rebuild_with_fk_off` helper the goal/habit/goal_check rebuilds use, with a 0-row count guard since
the table has never been written -- never a `DELETE`. New `logout_jti(jti, exp)` table.

`body/web.py`: `GET /login` and `/login/cuenta/start` (both start the flow), `GET /login/cuenta/callback`,
`POST /logout`, `POST /login/cuenta/logout-token` (accepted only from a loopback client address), `GET
/healthz` (200 "ok", both modes). A FastAPI lifespan calls `cuenta.ensure_configured()` so a misconfigured
hosted deploy refuses to start before serving any request.

`body/vincular_cuenta.py` (new): `python -m body.vincular_cuenta --csv <file> [--apply]` links an existing
person to a sub by `id_local`, dry run by default, idempotent, refuses to change a link already set.

`body/templates/sin_acceso.html` (new): standalone page (no app nav) with the Cuenta switcher link and a
sign-out link to Cuenta's own `/oauth/logout` -- never a 403 dead end. `body/templates/base.html`: "Cerrar
sesión" in the desktop rail nav and the mobile footer, hidden in local mode (`hosted()` is a new Jinja
global). `body/i18n/{es,en}.json`: `nav.logout`, `cuenta.down`, `sin_acceso.title/text/switch`.

`requirements.txt`: `PyJWT[crypto]==2.15.1`, `httpx==0.28.1` (both already installed; versions re-measured
from the venv, not assumed).

**Tests**: `tests/fixtures/cuenta_vectores.json` copied byte-for-byte from dev-protocol (verified by sha256)
via `git cat-file`. `tests/test_cuenta_vectores.py` runs `cuenta.verificar_claims` over all 30 vectors -- all
pass. `tests/test_cuenta_rp.py` (32 tests): a local fake RSA-keyed issuer exercises the full sign-in (new sub,
already-linked sub, Sin acceso), every listed refusal (state mismatch, missing/foreign flujo, reused attempt,
wrong nonce, token-endpoint error), sign-out, back-channel logout (by sid, by sub, replay,
typ/events/nonce/missing-sub-and-sid, non-loopback caller), expired session -> `prompt=none`, `return_to`
safety, local-mode parity, `/healthz`, and the startup refusal message. `tests/test_person_scope.py`: a new
hosted-mode case with two real Cuenta sessions (not `dependency_overrides`) proves the same person-scope
guarantee. `tests/test_cuenta_rp.py` also covers `vincular_cuenta`'s dry-run/apply/idempotent/refused behavior
and the `inicio_sesion` shape rebuild.

Gate run by the slice itself: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 210 tests, 1
failure (`RouteMappingTest.test_every_get_route_has_a_mapping_decision` in `tests/test_agente_registro.py`,
outside this slice's `touches`), 209 passing. The failure's cause: this slice's four new GET routes
(`/healthz`, `/login`, `/login/cuenta/start`, `/login/cuenta/callback`) have no entry in that test's
`ROUTE_TOOL` registry, which asserts every GET route in `body/web.py` carries an explicit tool-mapping
decision. Per BODY-CUENTA-SCHEMA-1's own precedent for this same situation, the slice stops GATE_RED, unmerged,
and does not write its own `DECISIONS.md` entry; BODY-CUENTA-RP-1-M below records the orchestrator's review,
the route-mapping decisions, the back-channel proxy-header fix, and the merge.

## BODY-CUENTA-RP-1-M -- lands BODY-CUENTA-RP-1, closes the route-mapping gap and the back-channel proxy gap (2026-11-06)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after reviewing the branch (15 files,
+1788/-20, re-measured against `main` and matching): `flujo` and `body_sesion` cookies host-only, `HttpOnly`,
`SameSite=Lax`, `Secure` on https; `state`/`nonce`/`verifier` in a `flujo`-keyed row; `client_secret_basic`;
logout with `logout_hint=sid` and no `id_token_hint`; durable `logout_jti`; `return_to` validated; the secret
read from the environment, never printed. The `acr` RANK comparison was accepted as written (cuenta-duna-v1
section 3.2 says "compared as a RANK, pwd < passkey" explicitly). Approved with two additions, both carried out
in this slice.

**Route-mapping decisions** (`tests/test_agente_registro.py`'s `ROUTE_TOOL`): the four auth GET routes added by
BODY-CUENTA-RP-1 -- `/healthz`, `/login`, `/login/cuenta/start`, `/login/cuenta/callback` -- each mapped to
`None` with a one-line comment ("sign-in plumbing, no data, no tool"), the same pattern already used for
`/goals/new` and `/sw.js`. This closes the gate's one failure without touching the registry's own guard logic.

**Back-channel loopback check made true behind Caddy, in `body/web.py`'s `/login/cuenta/logout-token`**:
behind a reverse proxy, a proxied request also arrives from `127.0.0.1`, so the existing client-address check
alone is not a real boundary. The route now also refuses with 404 any request carrying `X-Forwarded-For`,
`Forwarded`, `X-Forwarded-Host` or `X-Real-IP` (Caddy's `reverse_proxy` adds `X-Forwarded-For`), checked
*before* the request body is read, so a flagged request never reaches `cuenta.handle_backchannel_logout` and
ends no session. Two new tests in `tests/test_cuenta_rp.py`'s `BackchannelLogoutTest`:
`test_loopback_with_x_forwarded_for_refused_and_no_session_ended` (a loopback call carrying a valid logout
token plus `X-Forwarded-For` gets 404, and the session it names is still present afterward) and
`test_loopback_without_proxy_headers_and_valid_token_still_200` (the same call with no proxy header still ends
the session and returns 200, as before this slice). This check stands on its own; the entry-script slice will
also block the path in Caddy.

`main` had not moved since the branch was cut: `git rev-parse main` and `git merge-base
feat/body-cuenta-rp-1 main` both resolved to `5dfd9619359c8109ba880fb0ad2806e355c95714`, matching the branch's
recorded base, so no merge-forward was needed.

No schema or migration change (no edit to `body/db.py`'s `SCHEMA`); no cross-repo contract change. The two auth
routes' own existing behavior (200/303/400/403/404 bodies, redirects) is unchanged -- this slice only adds
registry bookkeeping and a stricter refusal condition on a back-channel route a browser never renders -- so no
customer-facing bytes either. All three of merge policy A's conditions pass, so this merges on green without a
separate approval step, beyond the orchestrator's own approval of the Tier 1 build above.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 212 tests, OK (210 at the branch tip with
one known failure, 2 added here; the fixed `RouteMappingTest` case plus the two new back-channel tests brings
the total from 209 passing to 212 passing), run once on the branch tip (`1c6f8bb60a7eb59b2eaadf21a96fa9ed7990028e`)
before this merge.

Merge commit: recorded in the next commit's git history as the `--no-ff` merge of `feat/body-cuenta-rp-1` into
`main`.

## BODY-CUENTA-VECTORES-2 -- re-copy Cuenta's shared vector file after Main added the acr-rank vector (2026-11-06)

dev-protocol's `contracts/cuenta-duna-v1/vectores.json` was regenerated at commit `dc698125` with a new case,
`id-valido-acr-passkey-minimo-pwd`: a passkey sign-in (`acr: "passkey"`) to a client whose `requiere_acr` is
`"pwd"`, expected `aceptar`. Its own reference verifier (`verificar_vectores.py` at the same commit) now
checks `acr` as a RANK (`RANGO_ACR = {"pwd": 0, "passkey": 1}`), rejecting only when the token's rank is
*below* the client's minimum -- never by exact-string equality, which would wrongly refuse this vector.

`tests/fixtures/cuenta_vectores.json` was overwritten with the exact bytes of that commit's file (copied via
`git -C ../dev-protocol show dc698125:contracts/cuenta-duna-v1/vectores.json`, piped through a Python
subprocess straight to disk so no byte passed through hand-transcription) -- 31 cases, confirmed by loading
the JSON and counting `casos` before touching anything else. `body/cuenta.py`'s `verificar_claims` already
compares `acr` by rank, not equality (BODY-CUENTA-RP-1's own design note says so explicitly), so no change was
needed there, and none was made -- this slice touches no file under `body/`.

`tests/test_cuenta_vectores.py`: `test_vector_count_matches_the_copied_fixture` now expects 31 (was 30), and a
new `test_acr_is_a_rank_not_an_equality` asserts `id-valido-acr-passkey-minimo-pwd` is accepted on its own,
independent of the full-fixture loop, so a future change that breaks only the rank comparison fails a test
that names the behavior. The existing full-fixture loop (`test_every_vector_matches_its_expected_result`)
already threads `requiere_acr=caso.get("requiere_acr")` into `cuenta.verificar_claims` -- that plumbing was
already in place from BODY-CUENTA-RP-1, so the loop's shape did not need to change.

No schema or migration change; no cross-repo contract change (the fixture is a copy of Cuenta's own published
test vectors, not a DTO this app defines); no customer-facing bytes (nothing under `tests/` is served to a
user). All three of merge policy A's conditions pass, so this merges on green without a separate approval
step.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 213 tests, OK (212 at the branch tip plus
the one new `test_acr_is_a_rank_not_an_equality` case), run once on the final tree before this merge.

## BODY-HOST-ENTRY-1 -- Body's deploy shape for duna-prod-1, text plus `--dry-run` only (2026-11-06)

Carries out BODY-HOST-OBS-1 (OBSERVED, 2026-11-06: no deploy path existed) and BODY-HOSTING-PLAN-1 step 8a,
built to Main's `host-deploy-v1` contract and its sensitive-data amendment. Nothing here ran on the host:
every file was written and checked as text, and the gate never executes a `.sh` file or the entry script.
Carlos installs the entry script himself, and every deploy still waits for his go.

New files, all under `deploy/host/`: the root-owned entry script `duna-deploy-body` (`status` /
`deploy [--dry-run]`, EUID-0 refusal and a fixed `PATH=` before anything else, a closed subcommand
allowlist with `backup` reserved for BODY-HOST-BACKUP-1 and anything else a usage line plus exit 2), its
sudoers line `duna-deploy-body.sudoers` (exactly `carlos ALL=(root) NOPASSWD:
/usr/local/sbin/duna-deploy-body`), the product script `body-deploy.sh` that it `exec`s after the pull
(builds `.venv` and installs the systemd units and the Caddy site as root, `runuser -u "$owner" --` for
every git/pip/venv call, rolls back to `--before-sha` -- captured before the pull, validated as 40 hex
here -- and restarts on any failed step), the five systemd units `body-web.service`,
`body-pull.service`/`.timer` (daily 09:30 America/Bogota, pull then shift import), `body-vida.service`/
`.timer` (Sundays 18:00, pull then `vida_export --current`, mirroring `scripts/install-daily-sync.ps1`'s
own module entry points), `Caddyfile.body` (`reverse_proxy 127.0.0.1:8770`, security headers, no `log`
directive, and `respond /login/cuenta/logout-token 404` so Cuenta's back-channel stays loopback-only),
`body.env.example` (every variable name from decision 7 of the slice brief, blank except the two fixed
URLs and the R2 bucket name), and `README.md` with Carlos's install lines. `CLAUDE.md`'s Tier 1 sentence
now opens with `deploy/` ("what runs as root on the host"), ahead of the Garmin adapter.

**Layout decided**: repo at `/srv/duna/repos/duna-body`, owned by Carlos's own host user (every git/pip/
venv call in both scripts runs as that owner via `runuser`, never as root); system user `duna-body`
(created by the entry script with `useradd --system --no-create-home --shell /usr/sbin/nologin` if
missing) runs the three long-lived/scheduled services and owns `/srv/duna/data/body` (= `BODY_HOME`),
mode 0700, created by the entry script and never deleted by it; env file at `/srv/duna/env/body.env`
(root:duna-body, 0640, written by Carlos, never read back by either script -- neither contains `cat`,
`echo`, `printf` or `source` against it). The web service binds `127.0.0.1:8770` only (no `--lan`); Caddy
is the only path in. The health check hits `/healthz`, confirmed already present (`git log -S healthz`
traces it to BODY-CUENTA-RP-1's own `d11d55c`), so the product script needs no fallback to `/`.

**What stays by Carlos's hand**: installing the entry script and its sudoers line (`sudo install` +
`sudo visudo -c`), writing `/srv/duna/env/body.env` from the example, the one-time copy of
`Caddyfile.body` before the very first deploy (so Caddy has somewhere to proxy to), and triggering every
`deploy` himself -- nothing here is wired to run on its own.

**Assumptions for Main to confirm**: the private GitHub repo name is `duna-body` (one constant changes if
Carlos names it otherwise); the host's main Caddyfile imports `/etc/caddy/sites/*`, as the README states.

`tests/test_host_deploy.py` (`tests._isolate` imported first) checks all of this at the text level, no
bash execution: the usage allowlist and exit-2 catch-all, the EUID check ahead of everything else
(including the `PATH=` reset), every `git -C` call in both scripts prefixed by `runuser -u "$owner" --`,
`--before-sha` captured before the pull and validated as 40 hex, the bare `exec` of the product script,
every `systemctl`/`install`/`useradd` wrapped by a `run()` helper that echoes instead of executing (pip's
own `install` subcommand excluded, since it is not the coreutils command the rule means), no dump of the
env file, no `--pty`, no `rm` anywhere in either script, the sudoers file's exact single line, the
README's install lines naming files that exist, each unit's `User=duna-body`/`EnvironmentFile=`/port
8770/no `--lan`, the Caddyfile's proxy target and backchannel 404, `body.env.example`'s exact name set
and blank-except-fixed values, and LF line endings on every file under `deploy/host/`.

No schema or migration change; no cross-repo contract change (nothing here defines or consumes a DTO
another repository mirrors). This touches root-run deploy code under Tier 1, so per this slice's own
spec it stops `AWAITING_APPROVAL` on the branch clean rather than merging on green, whatever merge policy
A's three conditions would otherwise say.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 241 tests, OK (213 at the branch tip
plus 28 new in `tests/test_host_deploy.py`), run once on the final tree.

## BODY-HOST-ENTRY-1-M -- land BODY-HOST-ENTRY-1 with seven fixes from the root-run review (2026-11-06)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after reviewing every file of
`feat/body-host-entry-1` against host-deploy-v1 and the sensitive-data amendment. Seven defects that would
bite on the host's first run, or weaken the amendment, were found and fixed here, each with a new text-level
test in `tests/test_host_deploy.py`:

1. **Exec bit.** Both scripts were mode 100644 in git (the repo is authored on Windows), so a host checkout
   could lose the executable bit entirely. `duna-deploy-body` now runs the product script as `exec /bin/bash
   "$PRODUCT_SCRIPT" --before-sha "$before_sha"` (the `--dry-run` line matches), so execution never depends on
   the bit at all; both scripts are also marked executable in the index (`git add --chmod=+x`, `update-index`
   itself not being a granted verb here), `100755` now.
2. **Health check retries.** The single `sleep 1` + one `curl` in `body-deploy.sh` is now a loop of up to 20
   attempts, 1 s apart, succeeding on the first `200` from `/healthz`; only after 20 failed attempts does it
   roll back.
3. **Rollback reinstalls dependencies.** `rollback_and_fail()` now re-runs the owner's `pip install -q -r
   requirements.txt` after `git reset --hard "$before_sha"`, before restarting the web service; if that pip
   install fails, it still proceeds to restart and reports it in the failure message rather than silently
   leaving the rolled-back checkout's dependencies mismatched.
4. **Caddy validated before reload.** After installing the site file, `body-deploy.sh` now runs `caddy
   validate --config /etc/caddy/Caddyfile --adapter caddyfile` before touching `systemctl`; on failure it
   removes only the just-installed `/etc/caddy/sites/body.caddy` and fails naming the step, without ever
   reaching `systemctl reload caddy`. This is the only `rm` in either script.
5. **UMask=0077.** Added to `body-web.service`, `body-pull.service` and `body-vida.service`, alongside
   `ProtectHome=yes`, so every file these services create is 0600, per the sensitive-data amendment's rule 1.
6. **No shift import on the host yet.** `body-pull.service`'s second `ExecStart` (`body.shift_import`) is
   removed -- shift files reach the host through an uploader that does not exist yet (`BODY-SHIFT-UPLOAD-1`,
   a later slice). The unit's `Description=` and `README.md` were updated to say so.
7. **Honest Caddy comment.** `Caddyfile.body`'s comment no longer claims the `/etc/caddy/sites/*` import is
   "confirmed with Main" -- it now says this is an assumption Main confirms in the walk-through. `README.md`
   gained a short "Host assumptions for Main to confirm in the walk-through" list: the import, `python3` on
   root's `PATH`, `caddy` and `curl` installed, and the repo name `duna-body`.

The `/healthz` deviation from the original spec text is accepted unchanged (BODY-CUENTA-RP-1 already landed
that route). No other code changed.

`main` had not moved since the branch was cut: `git merge-base HEAD main` and `git rev-parse main` both read
`9927d3a84b02256214fe42960ee30ac50f05529f`, matching the branch's recorded base, so no merge-forward was
needed.

`tests/test_host_deploy.py` gained 13 new tests (one per fix, plus one extra each for the exec-bit and health-
check-retry checks) and had one existing test (`test_no_rm_anywhere_in_either_script`) narrowed to
`test_only_rm_touches_caddy_dst`, since fix 4 now needs exactly one `rm`.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 254 tests, OK (241 at the branch tip plus
13 new in `tests/test_host_deploy.py`), run once on the final tree before this merge.

Slice commit: `bc7144df5a39cdabfa93e010e729425d5cbb8bb2` on `feat/body-host-entry-1`.

Merge commit: recorded in git history as the `--no-ff` merge of `feat/body-host-entry-1` into `main`
following this entry.

## BODY-HOST-ENTRY-2 -- align Caddy install, the venv check and the system user with Main's duna-prod-1 facts (2026-11-06)

Follows BODY-HOST-OBS-1 and BODY-HOST-ENTRY-1(-M). Main's walk-through answer replaces the host assumptions
BODY-HOST-ENTRY-1 wrote: the host is Ubuntu 24.04.4, so `python3` is 3.12; the main Caddyfile imports
`/etc/caddy/sites/*.caddy` (the `.caddy` suffix is required); the repo owner is read at runtime with
`stat -c %U`, as the scripts already did; and `duna-body` is a system user created with its own group.

**Caddy, the Lugares way.** `body-deploy.sh`'s `install_caddy_site` is replaced by
`install_caddy_site_if_changed`: it does nothing if `/etc/caddy/sites/body.caddy` already matches the repo's
`Caddyfile.body` byte for byte (`cmp -s`); otherwise it backs up the existing file to `.bak` (if one exists),
installs the new one, and runs `caddy validate --config /etc/caddy/Caddyfile`. On failure it restores the
`.bak` (or removes the new file when there was no backup), fails naming the step, and never reaches
`systemctl reload caddy`; on success it reloads. `rollback_and_fail()` now calls this same function instead
of the old unconditional installer, so a rollback's Caddy handling matches a normal deploy's. This replaces
the "remove on failure" logic BODY-HOST-ENTRY-1-M added.

**Missing `python3-venv`.** Before building the venv, `body-deploy.sh` runs, as the checkout owner,
`python3 -c "import ensurepip, venv"`. If that fails, it exits non-zero naming the one-time hand step --
`sudo apt install python3-venv` -- without touching `systemctl`, so the service is not restarted. Chosen
over a second install path through `uv`: one way to build the venv, and the gap stays visible instead of
being papered over.

**System user.** `duna-deploy-body`'s `ensure_system_user` now runs `useradd --system --user-group
--no-create-home --shell /usr/sbin/nologin duna-body`, so `chown duna-body:duna-body` can never fail for
want of the group.

**README and Caddyfile.** `README.md`'s "Host assumptions for Main to confirm" list is replaced by "Host
facts, confirmed by Main (2026-11-06)" (Ubuntu 24.04.4, the `.caddy` suffix, the Lugares install pattern,
the runtime-read owner, `duna-body`'s own group), plus the one-time `sudo apt install python3-venv` hand
step. `Caddyfile.body`'s comment no longer calls the sites-import path an unconfirmed assumption; it states
Main confirmed it, with the `.caddy` suffix.

`tests/test_host_deploy.py` gained: the `cmp -s` guard and the backup-before-install-before-validate
ordering, with reload unreachable past a failed validate and the rollback path reusing the same function
(`InstallCaddySiteIfChangedTest`); the venv check preceding `python3 -m venv`, naming `python3-venv`, and
not touching `systemctl`/`fail_step`/`rollback_and_fail` on failure (`VenvCheckTest`); `--user-group` on the
`useradd` call; and that neither script ever hardcodes the owner to `carlos`. One existing test
(`test_validate_failure_removes_only_caddy_dst_and_does_not_reload`) was widened to also require the
`mv`/`$CADDY_BAK` restore path, and the README/Caddyfile honesty tests were updated for the new wording.

No schema or migration change; no cross-repo contract or DTO change. But this changes bytes Carlos reads as
the deploy's own operator -- the README's host-facts section, the Caddyfile's confirmation comment, and the
new `python3-venv` refusal message -- so Merge Policy A's "no customer-facing bytes" condition trips. Per
ORCH-APPROVED-IS-BUILD-NOT-MERGE-1, `approved: yes` on this slice's header authorizes the build, not a merge
past that tripped condition, and the spec body does not say in words that this slice may merge anyway -- it
only says "merge unless merge policy says stop." So this stops `AWAITING_APPROVAL` on the branch clean,
for the same reason BODY-HOST-ENTRY-1 did.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 263 tests, OK (254 at the branch tip
plus 9 new in `tests/test_host_deploy.py`), run once on the final tree.

## BODY-HOST-ENTRY-2-M -- land BODY-HOST-ENTRY-2 unchanged (2026-11-06)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after reviewing the diff: the
`install_caddy_site_if_changed` function follows Lugares' pattern exactly (`cmp -s` guard, `.bak`, `caddy
validate`, restore or remove on failure, no reload after a failed validate); the venv check refuses with
"python3-venv is missing: run once on the host: sudo apt install python3-venv" before creating the venv;
`useradd` carries `--user-group`; `README.md` carries Main's host facts. Approved as written, merged
unchanged -- no code change in this slice.

`main` had not moved since the branch was cut: `git merge-base HEAD main` and `git rev-parse main` both read
`998a0a718d9420f1be33dabee4beec5f6b391614`, matching the branch's recorded base, so no merge-forward was
needed.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 263 tests, OK, run once on the final
tree before this merge (same count BODY-HOST-ENTRY-2 recorded; this entry's own run reconciled against it).

Slice commit: `aa83370c61166bcaa0f0f221f0f232e40bed457f` on `feat/body-host-entry-2`.

Merge commit: recorded in git history as the `--no-ff` merge of `feat/body-host-entry-2` into `main`
following this entry.

## BODY-PRIVACIDAD-1 -- consent, export and delete-all (2026-11-06)

Carries out BODY-HOST-OBS-1 (OBSERVED: no consent, export or delete existed) and BODY-HOSTING-PLAN-1 step 6,
to Main's sensitive-data amendment rule 6 (Ley 1581 dato sensible). Added `body/privacidad.py`: consent
version `consentimiento-v1` (es/en text in `body/i18n/*.json`, short and plain -- what Body keeps, why,
where, the person's rights, "this is information, not medical advice"); `export_person(conn, person_id)`;
`delete_person(conn, person_id)`.

Gate (`body/identity.py`, hosted mode only -- local mode never gates): after resolving a session,
`current_person_id` checks `privacidad.has_consented` for the current version; a page GET with none raises
`ConsentRequired`, which `body/web.py` turns into a 303 to `/privacidad/consentimiento?return_to=<path>`; a
POST answers 403. Only `/privacidad/consentimiento` itself is exempt -- `/healthz`, static files and the
other sign-in routes never call `current_person_id` at all, so they needed no exemption.

Routes (`body/web.py`): `GET`/`POST /privacidad/consentimiento` (accept stores the row and 303s to
`return_to`; decline runs the same sign-out as `/logout` -- nothing is kept beyond the person row Cuenta's
sign-in already created), `GET /privacidad` (Ajustes' new "Privacidad" row: consent date, download, delete),
`GET /privacidad/exportar` (`Content-Disposition: attachment; filename="body-<date>.json"`, no figure in the
URL), `POST /privacidad/borrar` (requires the typed word, `BORRAR` es / `DELETE` en, from i18n).

Export shape: `{"formato": "body-export-v1", "exportado_en", "persona", "hogar", "vida": {goals, habits,
habit_checks, goal_checks, plans, shifts, shift_batches}, "dias", "actividades", "sueno",
"sincronizaciones", "consentimientos", "ajustes"}`. Generic coverage, checked by
`ExportTest.test_every_person_table_is_exported_or_excepted`: every table with a literal `person_id` column
(found via `PRAGMA table_info` over `sqlite_master`) must be in `privacidad.FULLY_HANDLED_TABLES`,
`PARTIAL_EXPORT_TABLES` (`raw_payload` -- kind/date only, never the stored JSON payload) or
`NEVER_EXPORTED_TABLES` (`sesion`) -- a new person table that is in none of the three fails that test. This
walk also caught `body/household.py`'s `hogar_local` (not in `touches`, not edited, only called, the same
way `body/db.py` and `body/cuenta.py` are) -- it is lazily created, so both `export_person` and
`delete_person` run `household.SCHEMA` first to guarantee it exists before the generic discovery looks for
person tables.

Delete-all: one transaction, foreign keys ON. Deletes every row this person owns, clears
`habit.assignee_person_id` where it points at them, and removes every `habit_check`/`goal_check` tied to a
goal THEY created regardless of who logged it -- a shared household goal they made disappears for every
member too, matched by `DeleteAllTest.test_right_word_deletes_every_row_of_a_and_takes_bs_check_on_as_shared_goal`
(the other member's own goal, habit and daily metrics are untouched). Hosted redirects through Cuenta's
logout; local re-creates the one local person empty on the next request via `db.init`'s existing `INSERT OR
IGNORE`. `_borrar_fuera_de_bd(person_id)` is a named, currently-empty call point for BODY-TOKEN-ENCRYPT-1 to
add deleting this person's Garmin token.

Registry guard (`tests/test_agente_registro.py`): the three new GET routes added to `ROUTE_TOOL`, mapped to
`None` -- privacy plumbing the assistant never reads or triggers.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 276 tests, **2 failures**, run once on
the final tree:

- `tests.test_cuenta_rp.FullSignInTest.test_signed_in_person_sees_their_own_today_page`
- `tests.test_person_scope.HostedPersonScopeTest.test_two_sessions_see_only_their_own_goals_and_404_on_the_others_id`

Both fail for the same reason: each signs a hosted person in (or opens a session directly via
`cuenta.create_session`) and then asserts a normal page renders -- true before this slice, false now, since
a hosted person who has never accepted `consentimiento-v1` is exactly who the new gate is built to redirect.
This is the designed behavior in decision 2, not a bug; the two tests assert pre-consent-gate behavior from
BODY-CUENTA-RP-1 and BODY-PERSON-SCOPE-1. Neither `tests/test_cuenta_rp.py` nor `tests/test_person_scope.py`
is in this slice's `touches:`, and the instructions governing this dispatch are explicit that a diff stays
inside `touches:` and a file needed outside it means stop, not widen scope alone. The fix is small and
mechanical -- in each, call `privacidad.record_consent(conn, person_id)` for the signed-in person(s) before
the assertions that expect a normal page -- but it touches two files this slice was not approved to edit, so
it is left for the dispatcher to fold into a fast-follow (or to re-dispatch this slice with `touches`
widened to include them). Open follow-up: **BODY-PRIVACIDAD-GATE-FIX-1** -- update those two tests to call
`privacidad.record_consent` for their signed-in person(s) before the assertions that now expect a normal
page; not done here because neither file is in this slice's `touches:`.

Commits on `feat/body-privacidad-1`: implementation and ledger, not yet merged -- the gate above is red, so
no merge was attempted regardless of merge-policy classification.

## BODY-PRIVACIDAD-1-M -- land BODY-PRIVACIDAD-1 (2026-11-06)

Approved by the orchestrator under ORCH-CARLOS-DELEGATES-TIER-1-1 to fold in the fix named above as
BODY-PRIVACIDAD-GATE-FIX-1 and merge. `main` had not moved since the branch was cut (its tip and the
branch's merge-base both read `1227029b289a65681a4eecd0bd1fee91730b97e2`), so no merge-forward was needed.

In `tests/test_cuenta_rp.py`'s `FullSignInTest.test_signed_in_person_sees_their_own_today_page` and
`tests/test_person_scope.py`'s
`HostedPersonScopeTest.test_two_sessions_see_only_their_own_goals_and_404_on_the_others_id`, each now calls
`privacidad.record_consent(conn, person_id)` for the signed-in person(s) before the assertion that expects a
normal page -- the fix keeps each test's subject (session and person scoping) and only supplies the consent
the new gate requires. Added one new case,
`FullSignInTest.test_signed_in_person_without_consent_is_redirected_to_consentimiento`, asserting the gate's
own effect on sign-in: a freshly signed-in person with no consent row is redirected to
`/privacidad/consentimiento?return_to=...`. No other file changed.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 277 tests, OK, run once on the final
tree before this merge (276 recorded by BODY-PRIVACIDAD-1 plus the one new case above, all green).

Slice commit: `f5759815a3dbb91f706525cfd98fd90a9caab4bf` on `feat/body-privacidad-1`.

Merge commit: recorded in git history as the `--no-ff` merge of `feat/body-privacidad-1` into `main`
following this entry.

## BODY-HOST-BACKUP-1 -- off-host backup, encrypted to age, uploaded to R2 (2026-11-07)

Carries out `host-deploy-v1-amendment-sensitive-data` rule 3 and `BODY-HOSTING-PLAN-1` step 8b: a
consistent SQLite copy, encrypted to Carlos's `age` public key, uploaded to R2 bucket `duna-respaldo`
under `body/`, plaintext deleted; a daily systemd timer runs it; 30-day retention is an R2 lifecycle
rule (hand step); a restore check Carlos runs on his PC proves a backup opens. No Python dependency
added: the `age` CLI and curl's built-in `--aws-sigv4`; retention by an R2 lifecycle rule rather than
list-and-delete in bash.

**`body/backup.py`.** `snapshot(out_path)` opens `config.DB_PATH` read-only (`file:...?mode=ro`) and
writes a consistent copy with `sqlite3.Connection.backup()` -- never a plain file copy, which could
catch the live database mid-write -- mode 0600 (a no-op on filesystems without POSIX bits, which is
why its own test skips on non-POSIX). `check(path)` opens a decrypted copy, runs `PRAGMA
integrity_check`, and on `ok` prints the table count and each table's row count; neither path ever
prints a value from any row. `python -m body.backup snapshot --out <path>` / `check <path>`, exit 0/1.

**`deploy/host/body-backup.sh`**, run as root by `duna-deploy-body backup [--dry-run]` (no pull
first): reads `BODY_BACKUP_AGE_RECIPIENT` and the four `BODY_R2_*` names from
`/srv/duna/env/body.env` by parsing `KEY=value` lines for exactly those names -- never `source`s or
`cat`s the file, never prints a value. Refuses naming the exact hand step if `age` or `curl` is
missing (`age is missing: run once on the host: sudo apt install age`, matching the python3-venv
refusal's shape from BODY-HOST-ENTRY-2). Makes a scratch dir under
`/srv/duna/data/body/backup-tmp` (0700, `duna-body`); runs the snapshot as `runuser -u duna-body --`
with `BODY_HOME` set; `age -r "$recipient" -o <file>.age <file>`; deletes the plaintext immediately
and again in a `trap` on EXIT, so it never survives even a crash. Uploads
`body/body-<UTC timestamp>.sqlite.age` with `curl --fail --aws-sigv4 "aws:amz:auto:s3" -K - -T
<file> <endpoint>/<bucket>/<key>`, the R2 access key and secret fed to `-K -` by a `printf` piped
into curl's stdin -- a bash builtin, so the secret is never a process's argv, never echoed. Removes
the local `.age` after a successful upload; prints only the object key and size.

**Timer.** `body-backup.service` (oneshot, root, `ExecStart=/usr/local/sbin/duna-deploy-body
backup`) + `body-backup.timer` (daily 03:30 America/Bogota, `Persistent=true`). `body-deploy.sh`
installs both and enables the timer, folded into the existing `UNITS` array and `systemctl enable`
line the same way the pull/vida timers already are.

**Entry.** `duna-deploy-body backup [--dry-run]` execs `body-backup.sh` directly (no pull, unlike
`deploy`); `status` now reports `body-backup.timer`'s state and `body-backup.service`'s last run via
`systemctl show -p ExecMainExitTimestamp`.

**README.md** documents the flow, the `sudo apt install age` hand step, the R2 lifecycle rule (30-day
retention on bucket `duna-respaldo`, prefix `body/`, a hand step, not code), and the restore check
Carlos runs on his PC (`winget install FiloSottile.age`, `age -d -i duna-backup.key -o restore.sqlite
<file>.age`, `python -m body.backup check restore.sqlite`, then delete the plaintext copy).

**Found and fixed while gating:** `tests/test_backup.py`'s `SnapshotTest`/`CheckTest` patched
`config.BODY_HOME`/`config.DB_PATH` in `setUp` to point at a throwaway database but never restored
them, leaking into every test that ran afterward in the same process. The first full-gate run caught
this itself -- `tests.test_pipeline.PipelineTest.test_end_to_end_page`'s own guard (`config.DB_PATH`
must start under the isolate's `TMP`) failed because of the leak, not because of anything in
`test_pipeline.py`. Fixed with a `tearDown` that restores both. Also fixed: the `age`/`curl` missing
check was templated through a shared `$bin_name`, so the literal refusal message the decision
requires was never actually produced; split into two explicit checks. Both fixes are in the second
commit below.

**Merge policy.** No schema or migration change; no cross-repo contract or DTO change. But this
changes bytes Carlos reads as the deploy's own operator -- `README.md`'s new "Off-host backup"
section, `duna-deploy-body`'s usage and `status` output, and the new `age`/`curl` refusal messages --
so Merge Policy A's "no customer-facing bytes" condition trips, the same way it did for
BODY-HOST-ENTRY-2. The spec body does not say in words that this slice may merge past that condition,
so this stops `AWAITING_APPROVAL` on the branch clean, for the same reason BODY-HOST-ENTRY-2 did.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 309 tests, OK, skipped=1 (the
POSIX-only `chmod 0600` check, skipped on this Windows worker), run once on the final tree (277 at
the branch tip plus 32 new: 6 in `tests/test_backup.py`, 26 in `tests/test_host_backup.py`).

Commits on `feat/body-host-backup-1`: implementation (`f8a0e9f5dc49f3bcefe95cb62872e9327a4337d1`) and
the leak/message fix (`f2fa5417575d6fac74f02dd5e54f7db653e67bdc`), not yet merged -- awaiting the
orchestrator's review of the customer-bytes condition above.

## BODY-HOST-BACKUP-1-M -- land BODY-HOST-BACKUP-1 (2026-11-07)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after reviewing the root-run
backup script (`deploy/host/body-backup.sh`), `body/backup.py` and the unit/timer/entry wiring:
consistent snapshot via the sqlite backup API as `duna-body`; env parsed for five names only, never
sourced or printed; `age` to the public recipient; plaintext removed in an EXIT trap; R2 credentials
reach curl only through `-K -` fed by `printf` (never argv); object key `body/body-<UTC>.sqlite.age`;
both deviations (`git add --chmod`, the two literal refusal messages) accepted. Three fixes required
before landing, all inside the script and its text tests; approved with them.

**Three fixes, in `deploy/host/body-backup.sh`.** (a) The `cleanup_plain` EXIT trap now removes both
`$plain_file` and `$enc_file` (`rm -f "$plain_file" "$enc_file"`), so an upload failure cannot leave an
encrypted copy piling up in `backup-tmp`. (b) `curl` runs with `-sS` (quiet progress, errors still
shown) in both the real upload command and the echoed dry-run line, so the journal no longer carries a
progress meter. (c) `read_env_var` strips one pair of surrounding double or single quotes from the
value after the `KEY=value` split (systemd env files allow `KEY="value"`), still printing nothing.

`tests/test_host_backup.py` gained one text test per fix: the trap's body contains `$enc_file`; every
`curl` line carrying `--aws-sigv4` (the real command and the dry-run echo, exactly 2) carries `-sS`;
`read_env_var`'s body strips a matching pair of `"`/`'` and never echoes or printfs the raw value. No
other code changed.

**Merge.** `main` had not moved since the branch was cut: `git merge-base feat/body-host-backup-1 main`
and `git rev-parse main` both read `3f70dc8402624b99e45096e50df1c9b9da11b0c1`, matching the branch's
recorded base, so no merge-forward was needed. This slice's own diff is three internal fixes to the
backup script and its tests -- no schema change, no cross-repo contract change -- but one of them
(curl's `-sS`) changes the dry-run line's own echoed text, which is operator-facing output the same way
BODY-HOST-BACKUP-1's own entry classified. This spec's WORK section explicitly instructs "Merge
`--no-ff` into `main`" after the fixes and the gate, which this entry reads as the body's authorization
to merge past that condition, per the merge-policy-A rule that a spec's body can say so in words.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 313 tests, OK, skipped=1, run once
on the final tree before this merge (309 at the branch tip plus 4 new: one test per fix above).

Slice commit: `a2c275e2688931f8424c1ee08ff241d9bdbba6aa` on `feat/body-host-backup-1`.

Merge commit: recorded in git history as the `--no-ff` merge of `feat/body-host-backup-1` into `main`
following this entry.

## BODY-GARMIN-HOST-1 -- each hosted person connects their own Garmin account (2026-11-07)

Carries out `BODY-HOSTING-PLAN-1` step 7 and Carlos's own choice `BODY-PULL-LOCATION-1` ("the host
pulls"), approved under `ORCH-CARLOS-DELEGATES-TIER-1-1`. Built to the sensitive-data amendment rule
2: outside-account access encrypted at rest with AES-256-GCM, key from the env file, only the person
signs in, deleting the person deletes the token. Local mode (Carlos's PC, `python -m body.login`,
the token folder) is byte-for-byte unchanged.

**Encryption shape.** A new table, `garmin_cuenta(person_id PRIMARY KEY, token_cifrado BLOB, nonce
BLOB, conectado_en TEXT, ultimo_error TEXT)`, additive in `body/db.py`'s `SCHEMA`. `body/garmin_cuenta.py`
is the only module that touches it: AES-256-GCM via `cryptography`'s `AESGCM`, the 32-byte key read
from hex env `BODY_TOKEN_KEY`, a random 12-byte nonce per write, and associated data
`b"body-garmin:" + str(person_id)` so a ciphertext copied to another person's row fails to decrypt
(`cryptography.exceptions.InvalidTag`) rather than handing over their Garmin access. `ensure_configured()`
mirrors `cuenta.ensure_configured()`'s pattern: hosted mode with no key refuses to start, naming only
the variable, and `body/web.py`'s lifespan calls it alongside the existing Cuenta check. `ultimo_error`
holds an error CLASS name only (e.g. `NotSignedIn`), never a message or figure.

**The flow.** Measured in the installed `garminconnect` 0.3.20 before writing any of this: `Garmin(...,
return_on_mfa=True)`; `login()` returns `("needs_mfa", client_state)` when Garmin asks for a code,
`client_state` a JSON-serializable dict; `resume_login(client_state, code)` finishes it on a fresh
`Garmin` instance; `Tokens.loads(str)` **does exist** (`garminconnect/client/tokens.py`), so the
temp-file fallback the spec asked me to consider if it did not was never needed -- `Garmin.login()`
also already tells a stored path from an inline token string via its own `_looks_like_json` check, so
`_client(token_str)` just calls `api.login(token_str)` directly.

`body/garmin_adapter.py` (Tier 1, the only module importing `garminconnect`) gains `start_sign_in(email,
password) -> ("ok", token_str) | ("needs_mfa", client_state_json)` and `finish_sign_in(client_state_json,
code) -> token_str`, both using email/password/code once and never storing, logging or returning them.
`fetch_day(day, token_str=None)` keeps its exact prior behaviour and return type (`DayData`) when
`token_str` is `None` (local, unchanged); given a `token_str` it returns `(DayData, token_after)` instead,
where `token_after` is Garmin's possibly-refreshed value of that same token, for the caller to compare
and decide whether to re-encrypt.

**Web (hosted only; 404 in local mode, per `cuenta.hosted()`).** `GET /garmin` renders connected-since
date, not-connected, or the last error in words (never the stored class name verbatim -- a small mapping
in `body/web.py` turns it into a sentence, falling back to a generic one); the connect form (email,
`type=password`/`autocomplete="current-password"`) says plainly that Body passes the password to Garmin
once and keeps only the access Garmin returns, encrypted. `POST /garmin/conectar` calls `start_sign_in`;
on `"needs_mfa"` the `client_state_json` is kept server-side in a process-local dict keyed by a sha256 of
the session-token cookie, 10-minute TTL (checked, not swept in the background -- the dict is small and
per-process); the code form is rendered directly in that response, not a redirect. `POST /garmin/codigo`
calls `finish_sign_in`; a wrong code keeps the pending state for another try inside the same TTL window,
an expired or missing one is refused. `POST /garmin/desconectar` deletes the row. Every route scopes
through `identity.current_person_id` with no resource id in the URL, so one person can never reach or
change another's connection. Garmin exceptions always surface as one of two generic sentences, never
the exception text. `body/templates/settings.html` gained a «Garmin» row, hosted-only. The request-log
scrub already covers these routes (it logs method/route-template/status/duration only); a test confirms
it for `/garmin/conectar` specifically. The assistant's route registry (`tests/test_agente_registro.py`)
maps `/garmin` to no tool, like the other privacy/account-plumbing routes.

**The pull loop.** `python -m body.pull` branches on `cuenta.hosted()` at the top of `main()`; hosted mode
(`--all`/`--recompute` are local-only) loops over every `garmin_cuenta.every_person_id()`, decrypts,
calls `fetch_day(day, token_str=...)` per day (same per-day logic, same `PAUSE_S` between days), and
re-encrypts only if the token actually changed (`token != token_before`) -- confirmed by a test that the
stored nonce is untouched when it did not. A person whose token is rejected raises `NotSignedIn`;
`pull_hosted` catches it, records the class name in `ultimo_error`, and continues with the next person.
Prints counts only (`N ok, N failed, N days stored`). `deploy/host/body-pull.service` needed no change:
it already runs `python -m body.pull --days 2`, now per person as the decision asked.

**Privacy (`BODY-PRIVACIDAD-1` extended).** `garmin_cuenta` joins `PARTIAL_EXPORT_TABLES`: the export
shows `{"conectado_desde": <date>}`, built from `conectado_en` alone, never the ciphertext column.
`delete_person()` gained an explicit `DELETE FROM garmin_cuenta`, so the generic person-table delete
removes it as any other person-keyed table; `_borrar_fuera_de_bd`'s docstring now says it stays empty
because nothing about a Garmin connection lives outside the database any more.

**Deviations.** None from the spec's decisions. One thing the spec asked me to re-find came back
differently from what it flagged as a possibility: `Tokens.loads(str)` exists, so no temp-file
workaround was written.

**Tests.** `tests/test_garmin_host.py` (new, 26 tests): encryption round-trip, wrong AAD, wrong key,
`ensure_configured` in both modes, sign-in ok, MFA start/finish/wrong-code/expired/no-pending, desconectar,
cross-person isolation, the request-log scrub, the pull loop (one person failing leaves the other pulled,
a refreshed token re-encrypted, an unchanged one left alone, a person with no row skipped), and local
mode (routes 404, `fetch_day` unchanged both with and without a local token). All against a fake Garmin
client patched in as `garminconnect.Garmin` -- no network, invented demo data only.
`tests/test_privacidad.py` gained `GarminExportDeleteTest` (3 tests: export shows the date only, export
with no row is `None`, delete-all removes the row). `tests/test_agente_registro.py`: `/garmin` added to
`ROUTE_TOOL` as `None`. `tests/test_host_deploy.py` needed no change (`body-pull.service` is unchanged).

**Merge policy.** This diff adds a table to `body/db.py`'s `SCHEMA` (a schema change) and changes bytes
Carlos reads as a signed-in person -- the new `/garmin` page, the Settings «Garmin» row, and the new
`garmin.*` strings in both `body/i18n/es.json` and `en.json`. Two of Merge Policy A's three conditions
trip (schema, customer-bytes); the spec's body says to stop `AWAITING_APPROVAL` on a clean branch for a
Tier 1 + customer-facing slice unless the policy says otherwise, and it does not say otherwise here. So
this stops before merging, the same way `BODY-HOST-BACKUP-1` did.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 342 tests, OK, skipped=1 (the
POSIX-only `chmod 0600` check, unrelated to this slice), run once on the final tree (313 at the branch
tip plus 29 new: 26 in `tests/test_garmin_host.py`, 3 in `tests/test_privacidad.py`).

Slice commit: `b31387e9062904ee4157988e299dafb3c0bb4021` on `feat/body-garmin-host-1`, not yet merged --
awaiting the orchestrator's review of the schema and customer-bytes conditions above.

## BODY-GARMIN-HOST-1-M -- six fixes land BODY-GARMIN-HOST-1 (2026-11-07)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after review; six fixes, inside
`BODY-GARMIN-HOST-1`'s own files, then merge:

1. **Wrong MFA code keeps the code form.** `POST /garmin/codigo` used to redirect to `/garmin?error=garmin`
   on a rejected code, which sent the person back to the email/password form and lost the pending MFA
   state's context from their view. It now re-renders `garmin.html` with `mfa=True` and `form_error` set
   to the existing generic sentence, so the code input stays on screen for another try (the state itself
   was already kept, not popped, inside the same TTL window).
2. **Expired MFA states pruned.** `_MFA_STATE`'s only write (`garmin_conectar`, on `"needs_mfa"`) now
   calls a new `_prune_mfa_state()` first, dropping every entry older than `_MFA_TTL_SECONDS` before
   adding the new one -- the process-local dict no longer grows with abandoned sign-in attempts.
3. **The hosted pull survives any one person's failure.** `pull_hosted` caught only
   `garmin_adapter.NotSignedIn`; any other exception from Garmin or the adapter crashed the whole loop,
   leaving every later person unpulled. It now catches `Exception`, records `type(e).__name__`.
   `_pull_one_person` wraps its day loop in try/finally so a refreshed token is still stored even when a
   later day raises, and the days already saved before the failure are attached to the exception
   (`e.saved`) so `pull_hosted`'s `saved` total still counts them. A person whose `leer()` returns `None`
   (the row disappeared between listing and reading) is skipped quietly, never counted as ok.
4. **A successful pull clears the last error.** New `garmin_cuenta.limpiar_error(conn, person_id)`, called
   by `pull_hosted` after a person's days pull without an exception -- a transient failure no longer sticks
   on the Garmin page forever once the next pull succeeds.
5. **The token key must be exactly 32 bytes.** `garmin_cuenta._key()` and `ensure_configured()` both now
   decode `BODY_TOKEN_KEY` through a shared `_decode_key()` that refuses anything but 64 hex characters
   (32 bytes); the message names the variable and `openssl rand -hex 32`, never the value.
6. **A rejected connection offers reconnecting in place.** `garmin.html`: when connected and `ultimo_error`
   is set, the email/password form now renders above the "Desconectar" button (connecting again
   overwrites the row and `guardar` already clears the error, so no separate disconnect step is needed
   first). `_garmin_last_error_text`'s fallback changed from `garmin.error.generic` to a new
   `garmin.error.pull_failed` key (es: "La última sincronización con Garmin falló. Body lo intentará de
   nuevo mañana."; en: "The last sync with Garmin failed. Body will try again tomorrow.") -- `NotSignedIn`
   still reads as the existing `garmin.error.not_signed_in` sentence, and `garmin.error.generic` stays
   reserved for the sign-in form itself.

**Tests.** `tests/test_garmin_host.py` gained 10: the MFA-wrong-code re-render and its retry-within-TTL,
pruning an old MFA state on the next write, the wrong-key-length refusal in both `ensure_configured()`
and at use time, a generic mid-loop exception that still keeps a refreshed token and counts the day
already stored, a success clearing a previous error, `leer()` returning `None` not counted as ok, and
three for the reconnect-in-place template (pull-failure text with the form above Desconectar,
`NotSignedIn`'s existing sentence, and the error-free case showing only Desconectar). One pre-existing
test (`test_wrong_code_shows_generic_error_and_stores_nothing`) was renamed and rewritten in place for
the new re-render behavior rather than the old redirect.

**No other code changed.** The diff stayed inside `body/garmin_cuenta.py`, `body/pull.py`, `body/web.py`,
`body/templates/garmin.html`, `body/i18n/es.json`, `body/i18n/en.json` and `tests/test_garmin_host.py`, as
`BODY-GARMIN-HOST-1-M`'s `touches:` listed.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 352 tests, OK (skipped=1), run once on
the final tree (342 at the branch tip plus the 10 new tests above).

Slice commit: `423dd7ea90acff0c9e960a6a399eb3fddd585138` on `feat/body-garmin-host-1`. Merged `--no-ff`
into `main` as `f3c31adbdccf8103c1e9428823f29ce3e1abbb35`.

## BODY-DISPOSITIVO-1 -- device keys for a linked PC, the shift-upload and vida APIs (2026-11-07)

Carries out `BODY-SHIFT-UPLOAD-OBS-1` (OBSERVED: shifts enter only from a PC folder, no non-browser
credential exists in Body, the weekly vida file is written on the host where Personal cannot reach it,
and the host's `body-vida` unit runs `vida_export` for `db.local_person_id` -- wrong once several
people live on one host) under `ORCH-CARLOS-DELEGATES-TIER-1-1`. Design decided by the orchestrator: one
device key per linked PC carrying explicit scopes, rather than one key per purpose.

**The key.** `dispositivo(id, person_id, nombre, token_hash UNIQUE, alcances, creado_en, usado_en,
revocado_en)`, additive in `body/db.py`'s `SCHEMA`, plus an index on `person_id`. `body/dispositivo.py`
is the only module that touches it: `crear` returns a `bdy_`-prefixed key once
(`secrets.token_urlsafe(32)`) and stores only its sha256 hex; `resolver` looks the hash up, confirms
with `hmac.compare_digest`, refuses a revoked row, and stamps `usado_en` (date precision); `listar`
never returns the hash and excludes revoked rows; `revocar` sets `revocado_en` only on the caller's own
row (another person's id changes nothing). Scopes are a space-separated set from `{turnos:subir,
vida:leer}`.

**Resolving a device request.** `body/identity.py` gains `person_from_device(scope)`, a FastAPI
dependency factory: hosted only (local mode 404s before it is ever reached); no `Authorization: Bearer
<key>`, or an unknown/revoked key, raises `DispositivoNoAutorizado` (401, `WWW-Authenticate: Bearer`,
`{"error": "no_autorizado"}`); a live key without `scope` raises `DispositivoSinAlcance` (403,
`{"error": "alcance"}`). Neither is gated by consent -- a key exists only after the person has already
consented and signed in to mint it -- and neither ever redirects.

**Ajustes.** `GET /ajustes/dispositivos` (session-authenticated, hosted only, 404 in local mode) lists
each linked PC (name, scopes in words, created, last used) with a «Desconectar» button
(`POST /ajustes/dispositivos/<id>/revocar`, 303 back) and a «Conectar un PC» form (name, both scopes
checked by default). The POST response is a 200 render of the same page showing the new key once, with
a plain sentence to copy it now because Body cannot show it again; a later GET never shows it.
`settings.html` gained a «Conexión del PC» row, hosted-only, next to the Garmin one.
`tests/test_agente_registro.py`'s route registry maps this GET, and the new `GET /api/vida/semana`
below, to no tool.

**`POST /api/turnos`** (scope `turnos:subir`): the shift file as the raw JSON body, max 256 KB (413
beyond), the file name in an `X-Body-Archivo` header that must match `turnos-*.json` (400 otherwise).
`body/shift_import.py`'s `propose` is refactored into a shared `propose_data(conn, data, file_name,
person_id)`, used unchanged by the path-based CLI import and now by this route. A new
`refusal_motivo(exc)` classifies an `ImportRefused` into one of three reason classes -- `esquema` (a
missing `source.received`/`source.extracted_at`), `dia_malformado` (an unknown kind, a malformed day, a
missing or mismatched `start_local`), `turno_duplicado` (a duplicate slot, or an off day and a working
shift both present) -- so the route's 422 body (`{"error": "archivo_rechazado", "motivo": <class>}`)
never echoes `ImportRefused`'s own message, which carries a day. A successful call answers `{"lote":
<batch id>, "turnos": <count>, "nuevo": true|false}`; the person still confirms the proposed batch at
`/shifts`, unchanged.

**`GET /api/vida/semana`** (scope `vida:leer`): optional `?semana=YYYY-MM-DD` (a Monday), default the
week `vida_export`'s own `--current` picks -- pulled out into `vida_export.current_week_monday()` and
reused by both. Returns `vida_export.week_section(conn, monday, person_id)` as JSON with `Cache-Control:
no-store`; no figure ever reaches the URL.

**The host timer retired.** `vida_export.main()` now checks `cuenta.hosted()` first and, if set, prints
"hosted: the weekly Body section is served per person at /api/vida/semana" and returns 1, writing
nothing. `deploy/host/body-vida.service` and `.timer` are deleted (`git rm`); `body-deploy.sh` drops
them from `UNITS` and the `systemctl enable` line, and a new `remove_retired_units()` stops, disables
and removes a leftover `body-vida.{service,timer}` from an earlier deploy, if present, through the
existing dry-run `run` helper, before `daemon-reload`. `deploy/host/README.md` updated to match.

**Privacy.** `dispositivo` joins `PARTIAL_EXPORT_TABLES`: the export carries name, scopes and dates,
never `token_hash`. `delete_person()` gained an explicit `DELETE FROM dispositivo`.

**Deviations.** None from the spec's decisions.

**Tests.** `tests/test_dispositivo.py` (new, 26 tests): the module itself (key shape, hash-only storage,
resolver ok/revoked/unknown, `listar` excludes revoked rows and never returns the hash, `revocar`
cross-person no-op); the Ajustes page (key shown once, gone on a later GET, revoke only the caller's own
row); `/api/turnos` (every status the spec lists, a valid file scoped to the key's own person and
visible only at that person's `/shifts`, the same file twice answering `nuevo: false`, a malformed
file's 422 body holding no date string); `/api/vida/semana` (scope enforcement, each key seeing only its
own person's figures, `no-store`); `vida_export.main()`'s hosted refusal; the request log carrying no
Authorization value and no body text. `tests/test_privacidad.py` gained `DispositivoExportDeleteTest`
(2 tests). `tests/test_agente_registro.py`: `/ajustes/dispositivos` and `/api/vida/semana` added to
`ROUTE_TOOL` as `None`. `tests/test_host_deploy.py`: the retired vida unit/timer constants and their
test removed, `ServiceHardeningTest` no longer reads them, two new tests cover the retirement itself
(the unit's absence from `UNITS` and the enable line, and `remove_retired_units()` going through `run`),
and the `rm`-only-touches-`$CADDY_DST` test widened to also allow a retired unit's path. The existing
path-based shift-import tests (`tests/test_shift_import.py`) pass unchanged.

**Merge policy.** This diff adds a table to `body/db.py`'s `SCHEMA` (a schema change) and changes bytes
Carlos reads as a signed-in person -- the new `/ajustes/dispositivos` page, the Settings «Conexión del
PC» row, and the new `dispositivos.*`/`settings.dispositivos` strings in both `body/i18n/es.json` and
`en.json`. Two of Merge Policy A's three conditions trip (schema, customer-bytes); the spec's body says
nothing about merging past them, so this stops `AWAITING_APPROVAL` on a clean branch, the same way
`BODY-GARMIN-HOST-1` did.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 381 tests, OK (skipped=1, the
POSIX-only check, unrelated to this slice), run once on the final tree (352 at the branch tip plus 29
new: 26 in `tests/test_dispositivo.py`, 2 in `tests/test_privacidad.py`, 1 net in
`tests/test_host_deploy.py`).

Slice commit: `17e441b1bb8107b10b8865e2cf3bc53c8b482bc6` on `feat/body-dispositivo-1`, not yet merged --
awaiting the orchestrator's review of the schema and customer-bytes conditions above.

## BODY-DISPOSITIVO-1-M -- three fixes land BODY-DISPOSITIVO-1 (2026-11-07)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after review; three fixes, inside
`BODY-DISPOSITIVO-1`'s own files, then merge:

1. **vida-v1's `person` follows the Cuenta sub, not the literal local key.** New
   `vida_export.vida_person(conn, person_id)`: the person's `cuenta_sub` when Cuenta-linked, else
   `os.environ.get("BODY_PERSON_KEY", PERSON_KEY)` -- a local install with no Cuenta sign-in keeps
   today's value unchanged. `week_section`'s own `person` field and every `goal_checks[].person` now
   call it instead of reading the module constant directly. Carries out the worker follow-up
   `BODY-VIDA-PERSON-KEY-GENERIC-1`.
2. **`/api/turnos` refuses an oversized body before reading it.** A `Content-Length` header above
   `MAX_TURNOS_BODY_BYTES` now answers 413 immediately, with nothing read from the request. The route
   then reads `request.stream()` into a capped buffer, stopping and answering 413 the moment more than
   the maximum has arrived -- so a missing or lying `Content-Length` can no longer make it buffer an
   unbounded body.
3. **`GET`/`POST /ajustes/dispositivos` are never cached.** Both responses now carry
   `Cache-Control: no-store` (the POST response is the one that ever shows a new key).

**Tests.** `tests/test_vida_export.py` gained `VidaPersonTest` (3 tests): a hosted person with a
`cuenta_sub` gets the sub (in `vida_person` and in `week_section`'s `person`), two persons with
different subs get different values, and a local person with no sub keeps `PERSON_KEY`.
`tests/test_dispositivo.py` gained 4: the Ajustes GET and POST responses both carry `no-store`; an
oversized `Content-Length` against a tiny actual body answers 413 without the small body changing the
outcome (proving the header path, not the stream, refused it); a lying, under-reported `Content-Length`
against an actually oversized body still answers 413 (proving the streaming cap, not the header,
catches that case).

**No other code changed.** The diff stayed inside `body/vida_export.py`, `body/web.py`,
`tests/test_dispositivo.py` and `tests/test_vida_export.py`, as `BODY-DISPOSITIVO-1-M`'s `touches:`
listed.

**Merge policy.** This diff touches none of Merge Policy A's three conditions -- no schema or migration
change, no customer-facing bytes (the fixes are server-side request handling and an internal field's
source value, not new strings or screens), no cross-repo contract change -- so it merges on green
without waiting.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 388 tests, OK (skipped=1), run once
on the final tree (381 at the branch tip plus the 7 new tests above).

Slice commit: `952f1ec62f26c97dde0fc84bb9c7b9e84111c825` on `feat/body-dispositivo-1`. Merged `--no-ff`
into `main` as `8411e7952ebe22f8f4d06fb5e1599b906d7db24d`.

## BODY-PC-CLIENTE-1 -- the PC side of a hosted Body: link, upload shifts, fetch the week (2026-11-07)

Carries out `BODY-SHIFT-UPLOAD-OBS-1`'s PC-side follow-ups (`BODY-SHIFT-UPLOAD-CLIENT-1` and
`BODY-SHIFT-UPLOAD-INSTALL-1`, folded into one slice because neither is useful alone), approved by
the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1`, against the two device-key routes
`BODY-DISPOSITIVO-1` built -- restated verbatim from `body/web.py` as merged (re-found, not copied
from the spec):

- `POST /api/turnos` -- `Authorization: Bearer <key>` (scope `turnos:subir`), header
  `X-Body-Archivo` (file name, must match `^turnos-.*\.json$`), a raw JSON body capped at
  `256 * 1024` bytes (refused by `Content-Length` before any read, and again by the streaming cap).
  200 `{"lote", "turnos", "nuevo"}`; 400 bad file name; 413 too large; 422
  `{"error": "archivo_rechazado", "motivo": <class>}`; 401 `{"error": "no_autorizado"}` with
  `WWW-Authenticate: Bearer`; 403 `{"error": "alcance"}`.
- `GET /api/vida/semana?semana=YYYY-MM-DD` (scope `vida:leer`, same Bearer header). 200 the
  `vida_export.week_section` JSON with `Cache-Control: no-store`; 400 a malformed or non-Monday
  `semana`; 401/403 as above.

**`body/pc_cliente.py`** (new, shared). The link file `BODY_HOME/dispositivo.json` = `{"url",
"clave"}`, written with `chmod(0o600)` best-effort (the same pattern `body/backup.py`'s own snapshot
write already uses). `leer_enlace()` raises `EnlaceFaltante`, a plain refusal naming
`python -m body.conectar_pc` and never the key. `llamar(method, ruta, *, body=None, headers=None) ->
(status, json-or-None)` over `urllib.request` (stdlib only; TLS verified by default -- httpx stays a
dev-only test dependency), a 30 s timeout, `Authorization: Bearer` from the saved key, and the URL
refused unless it is `https://`, or `http://` to `localhost`/`127.0.0.1` (for local testing). The one
call that reaches a socket is `_abrir()`, a thin wrapper over `urllib.request.urlopen`; every test
patches that one name with a fake, so nothing in this slice contacts `body.duna.solutions`.

**`python -m body.conectar_pc [--url] [--olvidar]`**: the key is read with `getpass.getpass`
(hidden, never on argv, never echoed) and checked for the `bdy_` prefix before anything is saved or
sent anywhere; then checked with one `GET /api/vida/semana`: 200 -> "PC conectado."; 401 -> "Esa
clave no sirve o fue desconectada." and the link file is removed; 403 -> names the missing
permission (`vida:leer`). `--olvidar` deletes the link file.

**`python -m body.subir_turnos [--dir]`** (folder default `BODY_SHIFTS_DIR`, reusing
`shift_import._candidate_files` rather than re-globbing): every `turnos-*.json` whose sha256 is not
already in `BODY_HOME/turnos-subidos.json` is POSTed with its name in `X-Body-Archivo`. 200 records
the sha whether or not `nuevo` is true (the host already having it is still "known" to this PC);
422 prints the file name and the `motivo` class and does NOT record the sha, so a fixed file
retries on the next run; 401/403 stops immediately (writing what was recorded so far) and says to
reconnect. Prints counts only -- "N subidos, M ya estaban, K rechazados" -- never a file's date or
shift content.

**`python -m body.traer_semana [--semana YYYY-MM-DD]`**: one `GET /api/vida/semana`, written to
exactly the path `body/vida_export.py` writes locally (re-found: `BODY_HOME/vida/body-week-
<monday>.json`; `vida_export.current_week_monday()` reused for the default Monday rather than
re-derived), atomically (temp file + `os.replace`), `chmod(0o600)` best-effort. Prints only the
path.

**Installer, `scripts/install-daily-sync.ps1`**: new `[switch]$Hosted`. Refuses (`exit 1`) until
`BODY_HOME\dispositivo.json` exists, naming `python -m body.conectar_pc`; refuses (`exit 1`) without
`-ShiftsDir`. Registers "Body app shift upload" (daily, default `09:15`) running `body.subir_turnos
--dir <ShiftsDir>`, and "Body app weekly fetch" (Sundays, reusing `$SundayAt`'s existing `18:00`
default -- the weekly file's current time) running `body.traer_semana`; then unregisters "Body app
daily sync", "Body app weekly file" and "Body app web" if present, and its `Write-Host` summary says
what it registered and removed. **Deviation from the spec's exact wording**, recorded rather than
guessed past: decision 5 said to "re-find any existing time" for the shift-upload default before
falling back to 09:15; grepping this repository and its `*.md` files for an existing "Personal
extraction time" found none recorded anywhere outside the spec's own sentence, so the spec's stated
default (09:15) was used as given. Without `-Hosted`, the script runs the exact same logic as
before -- same `$At` default `09:30`, same three task names and actions -- confirmed by a test that
reads the text after the new `if ($Hosted) { ... }` block and finds all three original
`Register-ScheduledTask` calls unchanged and neither new task name present there.

**README**: new "Body en el servidor: conectar este PC" section (mint the key in Ajustes ->
"Conexión del PC", run `conectar_pc`, run the installer with `-Hosted`).

**Tests** (`tests/test_pc_cliente.py`, new, 33 tests, `tests/_isolate` imported first; every
network call is a fake patched onto `pc_cliente._abrir`): the link file holds the key and is
`0600` where the OS supports it (this run is on Windows, so that one assertion is skipped there);
the key never appears in captured stdout/stderr or in the request's own `full_url`; `conectar_pc`'s
source has no `--clave`/`--key` flag; `http://` to a non-local host is refused without writing the
link file or leaking the key; `conectar_pc`'s 200/401 (file removed)/403 paths; `subir_turnos`
uploads only files whose sha differs, records the sha on 200 regardless of `nuevo`, does not record
on 422 so a fixed file retries, stops at the first 401/403, and its printed output matches neither
`\d{4}-\d{2}-\d{2}` nor shift content; `traer_semana` writes the exact local path atomically (no
leftover `.tmp` file), prints only that path, and never prints a metric value from the payload; the
installer's text -- `-Hosted` registers the two new tasks and unregisters the three old ones,
refuses without the link file and without `-ShiftsDir`, and the non-hosted path is unchanged.

**Merge policy.** No schema or migration change (`body/db.py` untouched). No cross-repo contract
change (the wire is the one `BODY-DISPOSITIVO-1` already shipped, called here unchanged, not
redefined). Customer bytes: the new scheduled-task names, the installer's `Write-Host` summary, the
three new commands' own printed lines, and the README section are read only by Carlos running his
own PC tooling from a terminal -- never a screen or an i18n string the hosted app serves, the same
distinction `BODY-WEB-AUTOSTART-1` drew for its own installer/README changes ("not customer-facing
application bytes (no screen, no i18n string)"). None of Merge Policy A's three conditions trip, so
this merges on green without waiting.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .`. First run on this branch: RED (1
failure) -- `tests.test_pc_cliente.SubirTurnosTest.test_uploads_only_new_files` wrote two fixture
files with byte-identical content, so `subir_turnos`'s own sha-based dedup correctly skipped the
second file within that same run (`len(calls) == 1`, not the `2` the test expected) -- a bug in the
new test's fixture, not in `body/subir_turnos.py`; fixed by giving the two files distinct
`source.attachment` values. Final run: 421 tests, OK (skipped=1), run once on the final tree (388 at
the branch tip, per `BODY-DISPOSITIVO-1-M`'s own entry above, plus the 33 new tests here).

Slice commit: see `git log -1` on `feat/body-pc-cliente-1` before the merge below. Merged `--no-ff`
into `main`.

## BODY-PC-CLIENTE-2 -- refuse redirects, trim a pasted device key (2026-11-07)

Post-merge review of `BODY-PC-CLIENTE-1` found that `pc_cliente._abrir` called `urllib.request.
urlopen` directly, which uses urllib's default `HTTPRedirectHandler`: on a 3xx it re-sends the
original request's headers, Authorization included, to the redirect target. A redirect to another
host would carry the device key there. Body's own API routes never redirect, so refusing every 3xx
loses nothing and turns a would-be key leak into an ordinary error status.

`body/pc_cliente.py`: new `_SinRedirigir(urllib.request.HTTPRedirectHandler)` whose
`redirect_request` always returns `None`; a module-level `_opener = urllib.request.build_opener
(_SinRedirigir)` (subclassing the default handler means `build_opener` installs this one in its
place, not alongside it); `_abrir` now calls `_opener.open(req, timeout=TIMEOUT_S)` instead of
`urllib.request.urlopen`, and stays the one function tests patch for every other call. With no
handler accepting the redirect, urllib's own `HTTPDefaultErrorHandler` raises the `HTTPError` that
was already the error path, so `llamar` returns the 3xx status like any other error code --
unchanged for every caller, which already print an unexpected status by its number alone.

`body/conectar_pc.py`: the key read by `getpass.getpass` is now `.strip()`-ed before both the
`bdy_` prefix check and the call to `escribir_enlace`, so a pasted key with a trailing space or
newline is checked and saved without it.

**Tests** (`tests/test_pc_cliente.py`, +3): `RedirectRefusedTest` patches `urllib.request.
AbstractHTTPHandler.do_open` (not `_abrir`, so the real opener and its redirect handler run) to
serve one 302 to another host and asserts `llamar` returns `302` and that the handler is called
exactly once -- the redirect target is never requested; a second test asserts the opener's
`handle_error["http"][302]` list holds a `_SinRedirigir` instance and not a plain
`urllib.request.HTTPRedirectHandler`. `ConectarPcTest` gained a case where the fake typed key
carries a trailing space and newline and the saved link file's `clave` matches the trimmed key
exactly.

**Merge policy.** No schema or migration change. No cross-facing bytes: nothing printed or saved
changes shape, only what is accepted as a key and how a redirect status is reported internally. No
cross-repo contract change: the wire Body's hosted API serves is untouched; this only hardens how
the PC client reaches it. None of Merge Policy A's three conditions trip, so this merges on green
without waiting. Same observed-report as the slice it hardens: `BODY-SHIFT-UPLOAD-OBS-1`.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 424 tests, OK, run once on the
final tree (421 at `feat/body-pc-cliente-1`'s merge, plus the 3 new tests here).

Slice commit: see `git log -1` on `feat/body-pc-cliente-2` before the merge below. Merged `--no-ff`
into `main`.

## BODY-ASISTENTE-PROVEEDOR-1 -- the assistant's brain, server side (2026-11-07)

Carrying out `BODY-ASISTENTE-OBS-1`'s slice (b), under `ORCH-CARLOS-DELEGATES-TIER-1-1`. The one
part not the orchestrator's to approve -- sending health figures to the model provider -- Carlos
approved himself, typed 2026-11-06 ("Assistant: 1", `BODY-ASSISTANT-DATA-LEVEL-1`).

**Provider interface** `body/agente/cliente.py` (Tier 1, the only module that sends anything to a
model provider): `Proveedor.responder(sistema, mensajes, herramientas) -> Respuesta`, with
`Anthropic` (Messages API), `OpenAI` (Chat Completions) and `Gemini` (`generateContent`) adapters,
each taking an injectable transport so tests never touch the network. Messages and tools travel in
one generic shape (`{"rol", "bloques": [...]}`, blocks `texto` / `llamada_herramienta` /
`resultado_herramienta`) that each adapter converts to and from its own wire format. Usage is
normalised to DISJOINT counts at this boundary (agent-capabilities-v1 amendment 2): a vendor that
reports prompt tokens INCLUDING cached ones (OpenAI's `prompt_tokens`, Gemini's
`promptTokenCount`) has the cached part subtracted once; Anthropic's `input_tokens` already
excludes the cache. Selection by env: `BODY_AGENTE_PROVEEDOR` (default `anthropic`),
`BODY_AGENTE_MODELO` (default `claude-sonnet-5-5`), `BODY_AGENTE_ANTHROPIC_API_KEY` /
`BODY_AGENTE_OPENAI_API_KEY` / `BODY_AGENTE_GEMINI_API_KEY`. No key for the selected provider ->
`proveedor_configurado()` returns `None` (the assistant is "not configured"; the app still
starts). No adapter path ever logs a request or response body, a prompt, a tool result or a key --
only an error's class name or HTTP status, via `logging.getLogger("body.agente.cliente")`.

**Loop** `body/agente/bucle.py`: tools offered = `herramientas.disponibles(nivel)` for the
person's CURRENT `nivel_datos`; every call still goes through `herramientas.ejecutar(...,
nivel)` -- enforcement at dispatch, not only at offer. A tool error (unknown tool, wrong nivel,
bad params) becomes a `resultado_herramienta` block carrying `error`, fed back to the model, never
an exception shown to the person. At most `MAX_RONDAS = 8` model rounds; at most `MAX_LECTURAS =
5` *successful* tool reads -- a refused call costs no read budget, so the two caps are
independently reachable (a run of refused calls can exhaust the round cap without ever touching
the read cap, and vice versa). Past either cap, `ejecutar` raises `LimiteRondas` (carrying the
usage already spent) rather than guessing an answer; a failed provider call raises
`FalloProveedor` (same reason). Measured against `herramientas.disponibles('ninguno')`: it is NOT
literally empty -- `leer_turnos` and `leer_ajustes` carry nivel `'ninguno'` themselves, so they
stay offered even when the person's own data level is `'ninguno'`; the loop does not re-restrict
beyond `disponibles`/`ejecutar`'s existing rank check (see `deviations`).

**Conversations** `body/agente/conversacion.py`, tables `conversacion_agente(id, person_id,
creada_en, titulo)` and `mensaje_agente(id, conversacion_id, person_id, rol, contenido_json,
creado_en)`. Tool results ARE stored (the person's own data, on Body's own disk), so
`conversacion.historial()` rebuilds the exact history (including past tool calls and their
results) that `bucle.ejecutar` sends back to the provider -- server-stored history needs no
signing (agent-capabilities-v1 section 7). `verificar_propiedad` raises `ConversacionAjena` for an
unknown or another person's `conversacion_id`; `web.py` turns that into a 404.

**Endpoint** `POST /asistente/preguntar` (session-authenticated like every POST; hosted consent
gate applies via the existing `identity.current_person_id` dependency): JSON `{"pregunta": str
(1..2000 chars), "conversacion_id": int|null, "lugar": str|null}` -> an SSE stream
(`text/event-stream`, hand-written with `StreamingResponse` -- `sse-starlette` is not installed
and nothing installs in this slice) with events `texto` (`{"delta"}`), `herramienta`
(`{"nombre"}`), `fin` (`{"conversacion_id", "rondas", "motivo_fin"}`), `error` (`{"codigo",
"mensaje"}`, codigo one of `no_configurado` / `tope_diario` / `proveedor` / `limite_rondas`, the
sentence from `body/i18n`). `lugar` is accepted and currently unused (reserved for a later panel;
agent-ui-v1's `accion_en_pantalla`/places registry is not part of this slice). Every outcome,
including every error, writes exactly one `uso_agente` row before the stream ends.

**Usage and ceilings** `body/agente/uso.py`, table `uso_agente` with section 6's per-request
fields exactly (`person_id, creado_en, proveedor, modelo, entrada, salida, cache_lectura,
cache_escritura, rondas, motivo_fin, codigo_error, es_pregunta, superficie`). `precio_modelo
(proveedor, modelo, tipo, micro_usd_por_millon)` is seeded EMPTY -- no price invented here.
`verificar_tope`, checked BEFORE the first model round: the daily spend ceiling
(`BODY_AGENTE_TOPE_DIARIO_MICRO_USD`, default 2 000 000 micro-USD = $2) applies only when
`precio_modelo` has a row for ALL FOUR token kinds of the request's model; otherwise the COUNT
breaker applies (`BODY_AGENTE_CONTEO_DIARIO`, default 200 questions/person/day) -- amendment 5's
"nothing is ever costed at 0 because its price is missing." Per-person overrides as data,
`tope_agente(person_id, diario_micro_usd, conteo_diario)`, both nullable. Any failure inside
`verificar_tope` (including a closed/broken connection) raises `TopeRefusal` -- fails CLOSED, never
open. The monthly question count is only ever counted via the existing `uso_agente` rows (no
monthly ceiling exists, by design: amendment 5 says it never refuses, so nothing enforces it).

**Privacy** (`body/privacidad.py`): `conversacion_agente`, `mensaje_agente`, `uso_agente` and
`tope_agente` added to `FULLY_HANDLED_TABLES`; `export_person` returns them under a new
`"asistente"` key (conversations, messages, usage rows, the ceiling override); `delete_person`
deletes `mensaje_agente` before `conversacion_agente` (FK order) and both usage tables.
`precio_modelo` carries no `person_id` and needs no entry in any of the three export/delete sets
(it is not person data). The coverage test (`test_every_person_table_is_exported_or_excepted`)
and a dedicated `AsistenteExportDeleteTest` (export shows the seeded rows and only this person's;
delete-all removes all four) both pass.

**requirements**: `httpx==0.28.1` was already moved from `requirements-dev.txt` to
`requirements.txt` by `BODY-CUENTA-RP-1` (`cuenta.py` already uses it for Cuenta Duna's OIDC
calls) -- see `deviations`. Neither file was touched in this slice.

**Tests** (`tests/test_agente_cliente.py`, 11; `tests/test_agente_bucle.py`, 15;
`tests/test_privacidad.py`, +3): each adapter's disjoint-usage mapping from a scripted vendor
response, including the OpenAI/Gemini "prompt includes cached" normalisation; no key ->
`proveedor_configurado()` is `None`; a failed call (via a patched `httpx.post`, no real network)
logs only the error's class name, never the prompt, a tool result, or the key
(`self.assertLogs` on `body.agente.cliente`). A two-round tool conversation streams
`EventoHerramienta` then `EventoTexto`; the round cap (nine rounds of a refused, unknown-tool
call -- it never touches the read budget) and the read cap (six tool calls in one round)
independently; nivel `'ninguno'` still offers `leer_turnos`/`leer_ajustes` but a forced
`leer_hoy` call is refused by `ejecutar` and fed back as a tool error; nivel `'resumen'` hides
`leer_dia`/`leer_hoy`; another person's `conversacion_id` 404s, at both the `conversacion.py` and
the `/asistente/preguntar` layer; the count breaker refuses the 201st question of the day without
ever calling the fake provider; a fully-priced model over the spend ceiling refuses; an unpriced
model falls back to (and is bound by) the count breaker; a closed connection makes the ceiling
read fail closed; one `uso_agente` row carries the fake's exact disjoint counts; the `body.request`
log line for `/asistente/preguntar` never contains the question text.

**Merge policy.** Schema change: YES -- `body/db.py` gained five new tables
(`conversacion_agente`, `mensaje_agente`, `uso_agente`, `precio_modelo`, `tope_agente`). Customer-
facing bytes: YES -- new i18n strings (`agente.sistema`, `agente.error.no_configurado`,
`agente.error.tope_diario`, `agente.error.proveedor`, `agente.error.limite_rondas`) are returned
by the new endpoint, even though no panel calls it yet. Cross-repo contract: no other repository
mirrors this shape today (this slice is server-only; `agent-ui-v1`'s event names are a future
panel's concern, read here only for naming, per the spec's own note). Two of Merge Policy A's
three conditions trip, and the spec's own gate section says plainly: "Stop AWAITING_APPROVAL on
the branch clean (Tier 1 + customer-facing) unless merge policy says otherwise" -- it says nothing
otherwise. This slice stops on the branch, uncommitted to `main`, for review.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- run twice on the final tree.
First run: 453 tests, 1 failure (`test_host_deploy.EnvExampleTest`: adding
`BODY_AGENTE_OPENAI_API_KEY`/`BODY_AGENTE_GEMINI_API_KEY` to `deploy/host/body.env.example`, as
decision 1 originally asked, broke that file's own enumeration test in a module outside this
slice's `touches:` list). Fixed by leaving only `BODY_AGENTE_ANTHROPIC_API_KEY` in the example
file (the OpenAI/Gemini keys are still read from the environment by `cliente.py`; they are simply
not pre-declared, blank, in the example file -- see `deviations`), without touching
`tests/test_host_deploy.py`. Second run: 453 tests, OK (1 skipped, pre-existing). 424 tests at
`feat/body-pc-cliente-2`'s merge, plus 29 new here (11 + 15 + 3).

Branch: `feat/body-asistente-proveedor-1`, not merged. Awaiting review of the schema and the new
customer-facing strings.

## BODY-ASISTENTE-PROVEEDOR-1-M -- lands BODY-ASISTENTE-PROVEEDOR-1 with three fixes (2026-11-07)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after review (the review
covered `cliente.py`, `bucle.py`, `conversacion.py`, `uso.py` and the privacy coverage); three
fixes required before landing.

**Fix (a), key out of the URL** (`body/agente/cliente.py`): `Gemini._post_real` sent its key as
`params={"key": self.api_key}` -- a query parameter that any HTTP access log can carry. It now
sends it in the `x-goog-api-key` header, alongside `content-type`, with no `params` at all --
matching how Anthropic (`x-api-key`) and OpenAI (`authorization: Bearer`) already send theirs.
Test (`NoKeyInUrlTest`, `tests/test_agente_cliente.py`): a recording fake patched onto `httpx.post`
for all three adapters asserts the key string appears in no URL and no `params`, only in the one
named header.

**Fix (b), the person's own day** (`body/agente/uso.py`): `verificar_tope` compared `creado_en`
(UTC) against `LIKE '<UTC today>%'` -- a Bogota person's 11pm-1am window was bucketed by UTC's
calendar day, not theirs. `_rango_dia_local_utc` now reads `person.timezone` (default
`America/Bogota`, same column and default `web.py` already uses via `ZoneInfo`), builds [local
midnight, next local midnight) in that zone from `_ahora_utc()` (split out from `_ahora` so a test
can freeze "now"), converts both bounds to UTC and queries `creado_en` by range (`>= ... < ...`),
never `LIKE`. Test (`LocalDayBoundaryTest`): with "now" frozen at Bogota 2026-11-02 00:30 (UTC
2026-11-02 05:30), a row at Bogota 2026-11-01 23:30 (UTC 04:30, same UTC calendar day) is NOT
counted as today; a row at Bogota 2026-11-02 00:15 (UTC 05:15) IS.

**Fix (c), row-by-row pricing** (`body/agente/uso.py`): `_gasto_hoy_micro_usd` priced every row of
the day at the CURRENT request's `(proveedor, modelo)` prices, so a person who switched models
mid-day had yesterday's-model rows mispriced at today's model's rate. It now looks up each row's
OWN `(proveedor, modelo)` prices (cached per key within the call) and sums each at its own rate;
if any of today's rows carries a model with no full price, the function returns `None` and
`verificar_tope` falls back to the COUNT breaker for the whole check (amendment 5: never cost
anything at 0). Tests (`RowByRowPricingTest`): two rows under two different priced models sum to
exactly each model's own rate (a ceiling set between the correct sum and what pricing every row at
the current model's rate alone would give discriminates the fix); one of today's rows carrying an
unpriced model forces the count breaker.

No schema change (the existing `creado_en` index already supports a range query; `person.timezone`
already existed) and no new customer-facing bytes -- merge policy A's three conditions all clear,
so this slice merges on green without a further stop.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- 460 tests, OK (1 skipped,
pre-existing). 453 at `feat/body-asistente-proveedor-1`'s tip, plus 7 new here (3 + 4).

Merge: `--no-ff` into `main`.

## BODY-ASISTENTE-PANEL-1 -- the assistant people see: one shared panel on every screen (2026-11-07)

Carries out BODY-ASISTENTE-OBS-1's slice (c) to `agent-ui-v1` (the orchestrator's contract, read read-only at
`C:\Duna\body\dev-protocol\contracts\agent-ui-v1.md`; this session's sandbox could not open a path outside
`C:\Duna\body-app`, so R1-R6 were followed as the spec itself restated them in full, not re-read from the contract
file -- an `unknowns` item below). Touches the Tier 1 provider module only to add `Demo`, selectable only by
`BODY_AGENTE_PROVEEDOR=demo`, never a default; no other Tier 1 file in `web.py`/`cliente.py`'s surface changed.

**Mount** (decision 1): `env.globals["asistente_configurado"]` (`body/web.py`), the same idiom already used for
`hosted()`, calls `agente_cliente.proveedor_configurado() is not None`. `base.html` wraps the button, the panel and
`assistant.js` in `{% if asistente_configurado() %}`; not configured renders neither (`NotConfiguredNoButtonTest`).

**R1** (button): `.asis-btn`, fixed bottom-right, `var(--body)` (Body's own calm-green mark, not a loud colour),
`aria-label` from `asistente.abrir`. Below 1024 px the phone bottom tab bar is fixed at the viewport bottom (`.tabs`,
64 px + `env(safe-area-inset-bottom)`), so `.asis-btn`'s own bottom offset adds that height there; at >= 1024 px the
rail moves to the left and the button sits at the plain corner margin. `assistant.js` measures the button's own
rendered height and writes it to `--asis-pad`, which `main`'s `padding-bottom` now adds on top of its existing 96 px
(phone) / 48 px (desktop) reserve -- so the scrolling content's last row is never covered, on any viewport, without a
hand-measured breakpoint number. A `.asis-dot` shows while a question is in flight, or after an answer/error arrived
while the panel was closed, and clears when the panel opens. MEETS.

**R3** (sizes): < 768 px, `.asis-panel` is `position:fixed;inset:0` -- a full-screen sheet, with `#asis-cerrar` and an
`Escape` keydown listener both closing it. >= 768 px, three named stops (`lado` 400 px, `ancho` 50% of the viewport,
`completo` = viewport minus the 220 px rail when it is showing) cycled by `#asis-size`, one draggable inner edge
(`#asis-resize`, pointer events, `cursor:ew-resize`) continuously resizing between 400 px and 50% while in `lado` or
`ancho`, and the main column's `margin-right` tracks the open panel's width live via the `--asis-mr` custom
property -- so the page beside it reflows instead of being covered, while the 220 px rail itself never moves. Both
the chosen stop and the dragged width are written to `localStorage` (`body.asis.tamano`, `body.asis.ancho`) and read
back on the next page load, each access wrapped in `try`/`catch`. MEETS.

**R4** (scroll containment): `.asis-mensajes{overflow-y:auto;overscroll-behavior: contain}` (message list never
scroll-chains the page behind it); on a phone, opening the panel sets `document.body.style.overflow='hidden'` after
recording `window.scrollY`, and closing restores the exact position with `window.scrollTo`. MEETS.

**R5** (status and answer): one `.asis-status` line under the question, replaced on every `herramienta` SSE event via
`i18n.herramientas[nombre]` (nine phrases, one per read tool, e.g. `leer_tendencias` -> "Mirando tus tendencias…",
`leer_turnos` -> "Revisando tus turnos…"), falling back to the generic `agente.pensando` ("Pensando…") when a tool
name is missing from the map. The answer renders under the question as `texto` deltas arrive, and the tool-call
sequence folds into a collapsed `<details class="asis-steps">` labelled `asistente.como` ("Cómo lo hice · {n}
pasos", `{n}` substituted client-side with the step count) once the turn ends. An `error` event renders as one
`.asis-error` sentence, keyed by `codigo` through `i18n.errores` (`no_configurado`, `tope_diario`, `proveedor`,
`limite_rondas`, reusing `web.py`'s own four existing `agente.error.*` strings verbatim, plus a new fifth,
`agente.error.red`, for a client-side network failure the server never sees) with a `asistente.reintentar`
("Reintentar") button that calls `preguntar()` again with the same question text. MEETS.

**R6** (safe rendering): `assistant.js`'s `renderMD()` never assigns HTML -- every node is `document.createElement`
plus `textContent`, for paragraphs, `-`/`1.` lists, `**bold**` (via `inlineNodes()`), `#`/`##` headings, and pipe
tables (parsed into a real `<table>`, wrapped in `.asis-table-wrap{overflow-x:auto}` so a wide table scrolls inside
itself, never the page). Confirmed by `AssistantJsSafetyTest`: the file's text contains neither `innerHTML` nor
`eval(`. MEETS.

**The endpoint, as found** (`body/web.py`'s `/asistente/preguntar`, from BODY-ASISTENTE-PROVEEDOR-1 -- unchanged
here except for decision 8's model-name fix below): POST body `{pregunta, conversacion_id, lugar}`; SSE payloads
restated verbatim from `_evento_sse` calls in the running code -- `herramienta`: `{"nombre": ev.nombre}`; `texto`:
`{"delta": ev.texto}`; `fin`: `{"conversacion_id": conversacion_id, "rondas": resultado.rondas, "motivo_fin":
"texto"}`; `error`: `{"codigo": <no_configurado|tope_diario|proveedor|limite_rondas>, "mensaje": t(...)}`.
`assistant.js` posts with `fetch` (not `EventSource`, which cannot POST), reads the body with
`response.body.getReader()`, and parses `event:`/`data:` frames split on blank lines. No CSRF header exists anywhere
in this app (session auth is a `samesite=lax` cookie only, confirmed by grep); `assistant.js` sends none.
`conversacion_id` persists in `sessionStorage` across pages in the same tab; `#asis-nueva` ("Nueva conversación")
clears it and the message list.

**Decision 8, the `demo` provider** (`body/agente/cliente.py`): `Demo(Proveedor)` answers a scripted two-round
exchange -- round one calls `leer_ajustes` (the one read tool whose own `nivel` is `"ninguno"`, the lowest rank, so
it is offered at every `nivel_datos`); round two returns a fixed, invented Spanish paragraph with a list and a
two-row table, built entirely from static text, never a field of the real tool result. `Uso()` defaults to all
zeros, matching "its usage counts are zeros". `proveedor_configurado()` returns `Demo()` only when
`BODY_AGENTE_PROVEEDOR=demo`, checked before the real-provider dict and needing no API key; every other value falls
through to the existing anthropic/openai/gemini path unchanged, so `demo` is never reachable without the env flag
(`DemoProviderTest.test_demo_is_never_selected_without_the_env_flag`). `web.py`'s `asistente_preguntar` now sets
`modelo="demo"` when the provider name is `demo` (`agente_uso.registrar`'s rows would otherwise carry the unrelated
default model name "claude-sonnet-5-5" even though no such model answered) -- the one line in `web.py` beyond the
`asistente_configurado` global.

**Copy** (decision 9): `asistente.titulo` "Asistente", `asistente.placeholder` "Pregúntale a Body…",
`asistente.nota` "Body te responde con tus datos. No es consejo médico.", both es (tú) and en, added with full
key parity (`es.json`/`en.json` now have identical key sets).

**Tests** (`tests/test_asistente_panel.py`, 7; `tests/test_agente_cliente.py`, +3): every non-API, non-redirect,
signed-in `GET` page (today, how/{day}, day/{day}, shifts, trends, history, goals, goals/new, goals/{goal_id},
settings, privacidad, garmin, ajustes/dispositivos where hosted) renders `id="asis-btn"`, `id="asis-panel"` and the
hashed `assistant.js` `<script src>` when `BODY_AGENTE_PROVEEDOR=demo`, and none of the three when no provider is
configured; the panel markup carries `role="dialog"`, `aria-modal="true"` and the `#asis-size` control;
`body.css`'s text contains `overscroll-behavior: contain` and the `max-width:767.98px` full-screen rule for
`.asis-panel`; `assistant.js`'s text contains neither `innerHTML` nor `eval(`; a full demo exchange through
`/asistente/preguntar` streams `event: herramienta` before `event: texto` before `event: fin`, and `leer_ajustes`
appears in the stream. In `test_agente_cliente.py`: `Demo` is never selected without the env flag even with a real
key present for another provider; it needs no key itself; a `httpx.post` patched to raise `AssertionError` on any
call proves both of its scripted rounds make no network call.

**Merge policy.** Schema change: NO -- no table or migration touched. Customer-facing bytes: YES -- the panel itself
(a button and a dialog shown on every signed-in screen) and roughly twenty new i18n strings are new bytes a person
reads, even behind the `asistente_configurado()` gate that keeps them invisible until a provider (including `demo`)
is configured. Cross-repo contract: no -- `/asistente/preguntar`'s shape is unchanged from
BODY-ASISTENTE-PROVEEDOR-1, and `agent-ui-v1` is consumed here, not altered. One of Merge Policy A's three
conditions trips, and the spec's own gate section says to stop AWAITING_APPROVAL on customer-facing bytes unless the
spec's body says otherwise -- it says the orchestrator measures the panel in a browser before approving, i.e. it
says the opposite. This slice stops on the branch, uncommitted to `main`, for review.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- run once, foreground, on the final tree: 470
tests, OK (1 skipped, pre-existing). 460 at `main`'s tip (`61b97f0`), plus 10 new here (7 + 3).

Branch: `feat/body-asistente-panel-1`, not merged. Awaiting a browser measurement of the panel and review of the new
customer-facing strings.

## BODY-ASISTENTE-PANEL-1-M -- five fixes from the orchestrator's browser measurement (2026-11-07)

Approved by the orchestrator under `ORCH-CARLOS-DELEGATES-TIER-1-1` after review; five fixes, measured at 375 px and
1600 px in the Browser pane against the `demo` provider on scratch data.

**a. Enter sends on a computer.** `assistant.js` now binds a `keydown` listener on `#asis-input`: at >= 768 px
(`!esMovil()`) a plain `Enter` calls `e.preventDefault()` and `formEl.requestSubmit()`; `Shift+Enter` or any key on a
phone falls through to the textarea's own newline. Previously nothing intercepted `Enter` at all, so it only ever
inserted a line break and the question sat in the box.

**b. Lists without a blank line first.** `renderMD()`'s old list check required *every* line in a block to match
the `-`/`*`/`N.` marker, so a block that opened with plain text ("Así se ve una lista:\n- a\n- b") fell through to
the single-paragraph case and ran the dashes together. `tipoLinea()` now classifies each line as `ul`/`ol`/`p`, and
`renderLineas()` walks the block once, grouping consecutive lines of the same type into their own `<p>`/`<ul>`/`<ol>`
-- a list starts the moment a list-marker line appears, blank line or not.

**c. R5 order and a correct plural.** Two separate bugs, both visible only once a reply started streaming:
(1) `.asis-status{display:flex}`'s own class selector beat the UA `[hidden]{display:none}` rule on origin
precedence, so `turno.status.hidden = true` never actually hid the "Pensando…" line -- fixed with an explicit
`.asis-status[hidden]{display:none}` rule in `body.css`. (2) `agregarPasos()` always `appendChild`ed the folded
steps `<details>` at the end of the turn, after the answer; it now takes the turn's `answerEl` and
`insertBefore(det, answerEl)`s when one exists, putting the steps between the question and the answer. The plural
was a single hard-coded template (`"... {n} pasos"`); `asistente.como` in both `es.json`/`en.json` now carries
`"singular|plural"` in one string (`"Cómo lo hice · {n} paso|Cómo lo hice · {n} pasos"`), and `assistant.js`'s new
`formatComo(n)` splits on `"|"` and picks the first form only when `n === 1`. No template change was needed in
`base.html` (outside this slice's `touches`): the key still forwards as one opaque string.

**d. The conversation survives a page change.** A new `GET /asistente/conversacion/{conversacion_id}`
(session-authenticated via `identity.current_person_id`) calls a new `agente_conversacion.turnos_visibles()`, which
replays `historial()` and rebuilds only the visible turns -- `{"rol": "persona"|"asistente", "texto": str, "pasos":
int}` -- by treating a stored "usuario" message with no `"texto"` block (a tool result) as invisible, and folding
every assistant message between two questions into one answer, summing its `llamada_herramienta` blocks into
`pasos`. An unknown or another person's id raises the existing `ConversacionAjena` (unchanged from
BODY-ASISTENTE-PROVEEDOR-1's `verificar_propiedad`), mapped to a 404, same as the POST route. `assistant.js` now
fetches this on script load when `sessionStorage` already holds a `conversacion_id`, rebuilding the message list
from the returned turns; a 404 response clears the stored id. Adding this GET route trips
`test_agente_registro.py`'s `RouteMappingTest` (every GET route in `web.py` needs a tool-mapping decision or the
test fails on purpose) -- mapped to `None` there, one line, as the spec's own text for this fix anticipated
("registry guard: map it to `None`"). That file is outside this slice's declared `touches`; see Deviations below.

**e. Full size is actually full.** `anchoCompleto()` subtracted the rail's 220 px even though the panel's
`z-index` already sits above the rail, which combined with `right:0; left:auto` in `body.css` put the panel's left
edge at `x=220` instead of `0`; it now simply returns `window.innerWidth`. `aplicarTamano()` also set `--asis-mr`
(the main column's reflow margin) to the panel's own width for every size including `completo`, shrinking main to a
sliver; it now only does that for `lado`/`ancho` -- `completo` leaves `--asis-mr` at `0px` so main keeps its normal
width behind the overlay.

**Tests** (`tests/test_asistente_panel.py`, +11): `AssistantJsFixesTest` (6) and a CSS check for
`.asis-status[hidden]` are text-level, since the orchestrator re-measures the real behaviour in a browser after the
merge; `ComoPluralI18nTest` checks both `es.json`/`en.json` carry a distinct singular and plural form;
`ConversacionEndpointTest` (3) drives a full demo exchange through `TestClient`, then confirms the GET route returns
exactly the two expected turns with no raw tool result, 404s an unknown id, and 404s a second person's id (seeded
directly into the `person` table for the test).

**Deviations.** `tests/test_agente_registro.py` is not in this slice's declared `touches`, but a new GET route
mechanically requires an entry in its `ROUTE_TOOL` registry or the gate goes red; the spec's own text for fix (d)
named this exact requirement ("registry guard: map it to `None`"), so the one-line addition is made and called out
here rather than silently widened. The WORK section's ledger-text template said "three fixes" (carried over from
BODY-ASISTENTE-PROVEEDOR-1-M's own entry); this entry says five, matching the spec's defect list and this slice's
actual diff.

**Merge policy, re-judged against this diff.** Schema: no. Cross-repo contract: no. Customer-facing bytes: yes --
`assistant.js`, `body.css` and the `asistente.como` strings are read by every signed-in person once a provider is
configured, same as the parent slice. The spec's own body (step 6) says in words to merge `--no-ff` into `main` once
the gate is green, under the orchestrator's own `ORCH-CARLOS-DELEGATES-TIER-1-1` approval of these five named
defects -- Merge Policy A's authorization-to-merge condition is met even though the bytes condition trips.

Gate: `.venv/Scripts/python.exe -m unittest discover tests -t .` -- run once, foreground, on the final tree: 481
tests, OK (1 skipped, pre-existing). 470 at this branch's starting tip (`96b07b2`), plus 11 new here.

Merged into `main` with `--no-ff` immediately after this entry.
