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
