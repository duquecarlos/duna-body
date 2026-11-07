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
