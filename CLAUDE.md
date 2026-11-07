# Body app

Carlos's "human body" app, his idea, typed in Main on 2026-11-06: *"how do I connect my Garmin data metrics to an app [...]
the Garmin knows a lot of my metrics: heart rate, resting heart rate, heart rate variability, stress, exercise,
activities, movements [...] an app interface to see how my human body performs. What if I make some plans, some
adjustments, looking for different goals, health goals [...] The whole idea is that it's automatically linked with the
Garmin data."* Its name for now is **Body app**. It is for him first; he is "thinking of offering it as a Duna product
later", so build the template, not one person: goals, metrics and plans are data, never hardcoded to him.

## How this repository is worked

This repository is worked by slices dispatched from the Body orchestrator at `C:\Duna\body\dev-protocol`. The plan is
`ROADMAP.md`. The design rules are `docs/DESIGN.md`. The ledger is `DECISIONS.md`, append-only.

## Starting direction, proposed by Main and accepted 2026-11-06

* **Data, now:** the open-source `python-garminconnect` library, running locally on his PC, pulls daily figures: resting
  HR, HRV, sleep, stress, Body Battery and activities. **Carlos signs in himself and the library keeps a token. Never
  type, store or ask for his password.** It is unofficial and can break, so keep it behind one adapter module.
* **Data, as a product:** the official Garmin Connect Developer Program (Health API) needs a business application and
  Garmin's approval. Apply when the app becomes a Duna product.
* **The idea only he has:** he works rotating shifts, including nights, at Enel. His shift schedule comes by email
  (the "TURNOS <MES> A <MES>" mails from his team lead). Correlating shifts with sleep, HRV, stress and Body Battery
  shows what a night-shift week costs and how long recovery takes. Shift data reaches this app through his Personal
  assistant session (C:\Personal), never by reading his mail from here.
* **What the app does:** a daily readiness view, weekly trends, and goals with a plan (for example lower resting HR,
  better HRV, sleep before night shifts), plus a weekly check of whether the plan is working, using his own numbers.
* **Health data is sensitive:** keep it local or on Duna's own host; never in URLs or logs; no third-party analytics.
* His phone is an Android, a Samsung S25 Ultra.

## Privacy, for every session here

Never type, store, read or ask for a password; Carlos signs in himself and only the token is kept, in `BODY_HOME`
(default `~/.body-app`), outside the repo. No health figure in a URL, a log, a commit, a ledger entry or a report;
jobs print counts and dates only. No third-party analytics. Mocks and fixtures use invented demo data.

## Tests never touch real data

Every test module imports `tests/_isolate.py` first (a test once wrote fake data into the real database, 2026-11-05).
A new test module without that import is a defect.

## The gate

Exactly this command, from the repository root, its result read from the process:

    .venv/Scripts/python.exe -m unittest discover tests -t .

pytest is not installed in `.venv` today, so `unittest` is the runner.

## The trunk is `main`

Slices branch from `main` and merge back to `main` with `--no-ff`.

## Tier

Tier 1 slices run in a separate read-only session with no writes and no git mutation: the Garmin adapter
`body/garmin_adapter.py` (the only module that may import garminconnect), Garmin sign-in and token handling
`body/login.py`, where health data and the token live `body/config.py`, the SQLite schema and its migrations
`body/db.py`, the export that hands health data to another app `body/vida_export.py`, the test isolation guard
`tests/_isolate.py`, which network address the web app listens on `body/listen.py`, the import that brings
another app's shift files in `body/shift_import.py`, and the client that will send anything to a model provider
`body/agente/cliente.py` (the only module that may do so, once it exists).

Everything else in this repository is Tier 2.
