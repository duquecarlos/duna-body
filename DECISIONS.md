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
