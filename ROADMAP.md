# Body app — roadmap

Proposed 2026-11-06 by the Body app orchestrator. Stack approved by Main under Carlos's trust (2026-11-06).
Status (2026-11-06, evening): **slice 1 done on real data** (Carlos signed in; 2 days, 12/12 metrics, activities).
**Design round 1 open:** principles in `docs/DESIGN.md`, research in `docs/research/`, mock in `design/mocks/v1/`.
Waiting for Carlos's notes on the mock and his answers to the goal questions.

Implementation notes (slice 1): data and token live in `BODY_HOME` (default `~/.body-app`), not in `data/` in the repo.
The page is served by plain FastAPI HTML (no template engine), local by default, `--lan` for the phone; measured at 375 px.

## Stack (proposed)

| Part | Choice | Why |
|---|---|---|
| Garmin data | `python-garminconnect`, behind one adapter module (`body/garmin_adapter.py`) | Works today on his PC. Unofficial, so only one file knows it exists; swap for the official Health API later. |
| Sign-in | Carlos runs `python -m body.login` himself; the library saves a token in his user folder (outside the repo) | We never type, store or ask for his password. |
| Storage | SQLite, one local file (`~/.body-app/body.db`, outside the repo) | Health data stays on his PC. Easy to move to Duna's host later. |
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

## Scope — the app (design round 1, 2026-11-06)

Four screens, one question each (see `docs/DESIGN.md`): **Hoy** (how you arrive today + the shift ahead + today's
plan), **Turnos** (cost of a night week, ≈ days to recover, incoming schedule to confirm), **Tendencias** (each
metric vs. your 30-day baseline, night shifts shaded), **Metas** (goal = metric + target + date + plan items that can
depend on the shift; weekly "¿está funcionando?"). Plus **Día** as the drill-down every figure leads to.

Out of scope for now: medical advice, food logging, manual metric entry, social features, any cloud analytics.

## Plan — waves (each ends with Carlos testing on his phone)

| Wave | What | Done when |
|---|---|---|
| 0 ✓ | Slice 1: sign-in, pull, SQLite, plain day page | Real day shows (2026-11-06) |
| 1 | **Data base**: 90-day backfill; daily sync installed; sleep stages + sleep need + Body Battery charge parsed; timezone America/Bogota; baselines (30-day mean and normal range per metric) | 90 days in, baselines computed, tests |
| 2 | **Design system + shell**: Duna tokens, Hanken Grotesk, light/dark without flash, bottom tabs / left rail, interaction states (ui-interaccion-v1), return-to-same-spot, installable PWA on the S25 over LAN | Shell passes 375 px + measured hover/focus checks |
| 3 | **Hoy + Día** on real data, every figure drills to its day and back | Carlos uses it a morning |
| 4 | **Turnos**: import Personal desk JSON (`C:\Personal\enel\turnos`, FORMATO.md) with one-tap confirm; strip chart; night-vs-day comparison; ≈ recovery days with "cómo lo calculo" | First real schedule imported and confirmed |
| 5 | **Tendencias**: per-metric 7/30/90 with baseline band and night shading; all 12 metrics | |
| 6 | **Metas**: goals, plan items with shift context, daily check-off on Hoy, weekly check in words | His real goals entered from his answers |
| 7 | **Assistant** to agent-ui-v1 (read tools for every screen; proposes plan changes, he confirms) and tutorials-v1 | |
| 8 | **Duna product path**: Cuenta sign-in (`body` client), Duna host subdomain, Garmin Health API application, multi-person | Decision by Carlos |

Each wave: census → mock (Se mantiene / Cambia / Nuevo from round 2) → build → phone test → rules back to DESIGN.md
→ transferable rules to the hub (once "body" is on the product list).

## Horizon — "Duna personal" (Carlos, 2026-11-06)

Carlos: a global Duna for his whole life ("Duna human, automate your life"), joining the pieces that already exist
apart: Body, Finanzas Personal space, the Personal desk (C:\Personal), Lugares, his company. Later, not now. Body
should be built so it can be one space of that: person-scoped data, Cuenta-ready identity, data fed by the Personal
desk, a summary card other apps can read. Raised with Main.

## Open questions for Carlos (goals model)

1. Which 1–3 health goals matter most right now?
2. Is there a number you would like to reach, and by when?
3. Around night shifts, what hurts most (sleep, energy, mood, training, food) and what have you already tried?
4. Do you train for something specific, and do you want plans to touch training, sleep timing, or both?

## Decisions log

- 2026-11-06 — Starting direction accepted (Main): `python-garminconnect` locally now, official Health API later; shifts via Personal session.
- 2026-11-06 — Carlos: start the serious design work on Duna principles; research → principles → scope → plan →
  mocks → build. Round 1 mock delivered (demo data). Idea logged: "Duna personal" horizon.
- 2026-11-06 — Main, under Carlos's trust: build slice 1 up to sign-in; responsive from day one (375 px), PC/LAN only;
  goals modelled generically with nothing personal seeded; shift schedule is a first-class input (`shift` table,
  `plan_item.shift_context`). Goal questions wait for Carlos.
