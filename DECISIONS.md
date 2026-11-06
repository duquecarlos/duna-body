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
