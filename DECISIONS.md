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
