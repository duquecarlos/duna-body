# Body app — roadmap

Proposed 2026-11-06 by the Body app orchestrator. Status: **waiting for Carlos's answers** (stack OK? goals?).

## Stack (proposed)

| Part | Choice | Why |
|---|---|---|
| Garmin data | `python-garminconnect`, behind one adapter module (`body/garmin_adapter.py`) | Works today on his PC. Unofficial, so only one file knows it exists; swap for the official Health API later. |
| Sign-in | Carlos runs `python -m body.login` himself; the library saves a token in his user folder (outside the repo) | We never type, store or ask for his password. |
| Storage | SQLite, one local file (`data/body.db`, git-ignored) | Health data stays on his PC. Easy to move to Duna's host later. |
| Model | Metrics, goals, plans and shifts are rows of data, not code | Template for a future Duna product, not one person. |
| App | Python (FastAPI) serving a simple web page; opens on PC and, later, on his phone (Samsung S25 Ultra) over his home network or Duna's host | One language end to end; no app store; installable as a web app on Android. |
| Daily sync | A Windows scheduled task runs the pull once a morning | "Automatically linked" without him doing anything. |
| Privacy | No health values in URLs or logs; no third-party analytics | Sensitive data. |

## Slice 1 — one real day, end to end (before any screen design)

1. Repo skeleton, `.gitignore` for `data/` and tokens.
2. Carlos signs in once himself (`python -m body.login`); token saved locally.
3. `python -m body.pull --date <yesterday>` fetches: resting HR, HRV, sleep, stress, Body Battery, steps, activities.
4. Saves the day into SQLite (raw JSON kept too, so nothing is lost while the model settles).
5. One plain page shows that day's numbers.

Done when: yesterday's real numbers from his watch show on the page, and nothing sensitive is in the repo or logs.

## Next slices (rough order)

2. Backfill the last 90 days; daily scheduled pull.
3. Daily readiness view + weekly trends.
4. Goals and plans from Carlos's answers; weekly "is the plan working?" check.
5. Shifts: schedule arrives from the Personal assistant session (C:\Personal), never by reading mail here; shift-vs-recovery view (cost of a night-shift week, days to recover).
6. Phone access.
7. Later, if it becomes a Duna product: apply to the Garmin Connect Developer Program (Health API); add "body" to the hub product list.

## Open questions for Carlos (goals model)

1. Which 1–3 health goals matter most right now?
2. Is there a number you would like to reach, and by when?
3. Around night shifts, what hurts most (sleep, energy, mood, training, food) and what have you already tried?
4. Do you train for something specific, and do you want plans to touch training, sleep timing, or both?

## Decisions log

- 2026-11-06 — Starting direction accepted (Main): `python-garminconnect` locally now, official Health API later; shifts via Personal session.
