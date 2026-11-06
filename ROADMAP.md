# Body app — roadmap

Proposed 2026-11-06 by the Body app orchestrator. Stack approved by Main under Carlos's trust (2026-11-06).
Full Garmin history loaded 2026-11-06: 871 days (2024-06-19 → today), 391 activities.
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
| 1 | **Data base**: 90-day backfill; sleep stages + sleep need + Body Battery charge parsed; timezone America/Bogota; baselines (30-day mean and normal range per metric); **readiness score v1** (estimated, versioned) | 90 days in, baselines computed, tests |
| 2 | **Design system + shell**: Duna tokens, Hanken Grotesk, light/dark without flash, bottom tabs / left rail, interaction states (ui-interaccion-v1), return-to-same-spot, installable PWA on the S25 over LAN; **i18n es-CO (default) + en**, language as a person setting | Shell passes 375 px + measured hover/focus checks, both languages |
| 3 | **Hoy + Día** on real data with ≈ readiness hero and "cómo lo calculo"; every figure drills to its day and back | Carlos uses it a morning |
| 4 | **Turnos**: import Personal desk JSON (`C:\Personal\enel\turnos`, FORMATO.md) with one-tap confirm; strip chart; night-vs-day comparison; ≈ recovery days with "cómo lo calculo" | First real schedule imported and confirmed |
| 5 | **Tendencias**: first pass done 2026-11-06 on the whole history (871 days since 2024-06-19): last 30 days vs your history, a monthly line over your normal range, your typical week. Second pass: 7/30/90/History switch (vs the period before), tap a month → its days → tap a day → Día; missing days break the line; night shifts shaded (ready, shows once shifts are imported). Next: all measures | |
| 6 | **Metas**: goals, plan items with shift context, daily check-off on Hoy, weekly check in words | His real goals entered from his answers |
| 7 | **Assistant** to agent-ui-v1 (read tools for every screen; proposes plan changes, he confirms) and tutorials-v1 | |
| 7b | **Connections**: links out to other Duna apps where they hold the answer (night week → extra pay in Finanzas Personal); a read-only Body summary for other apps and for a future "Duna personal" | One working link each way with Finanzas |
| 7c | **Hogar — linked people, shared plans** (Carlos, 2026-11-06): each person has their own account and data; two people can link as a household (`household`, `household_member`); a goal/plan can belong to a person or to the household, with plan items per member (e.g. "Kilimanjaro / El Cocuy prep" for both). Each sees only their own health numbers unless they choose to share a figure. Aligned with Finanzas `hogar` and Lugares shared memories | Two accounts linked, one shared plan with each person's own check |
| 8 | **Duna product path**: Cuenta sign-in (`body` client), Duna host subdomain, Garmin Health API application, multi-person | Decision by Carlos |

Each wave: census → mock (Se mantiene / Cambia / Nuevo from round 2) → build → phone test → rules back to DESIGN.md
→ transferable rules to the hub (once "body" is on the product list).

## Plan — Hogar: linked people and shared plans (Carlos, 2026-11-06)

Carlos: his girlfriend gets her own user, and their accounts are linked "because we're like a starting family".
Same in Finanzas. Like the shared memories in Lugares, they can have **the same plans and projects**. Body example:
a joint plan to get ready for their next adventure (Kilimanjaro; "Kukui" probably El Cocuy, to confirm).

Model (person-scoped already; this adds):
- `household(id, name)`, `household_member(household_id, person_id, role)`. Joining needs both people's consent.
- `goal.owner` = a person OR a household; plan items assigned to one member or to both; each member's own weekly
  check (her numbers vs her baseline, his vs his).
- **Privacy by default:** health numbers stay private to their person; sharing is per figure, chosen by its owner
  (e.g. "share my readiness with the household"), and can be withdrawn.
- An adventure plan is a goal with a date and a target (e.g. "climb Kilimanjaro, Feb 2027") and plan items such
  as weekly long hikes, altitude prep, sleep before travel; it can link to the trip in Lugares and the budget in
  Finanzas (rule 17).
- Coordinated with Finanzas (`hogar` entity, FIN-PERSONAL-INTAKE-1) and Lugares through Main / the hub, so a
  household means the same thing in every Duna app.
- **Main, 2026-11-06:** working hypothesis: one household = a group of personas in **Cuenta Duna**, with both people's
  consent; each app maps it to its own space. Censuses running (CUENTA-API-HOGAR-CENSO-1, LUGARES-HOGAR-CENSO-1,
  Finanzas). Main sends the final shape. **Until then, Body's household lives behind one module
  (`body/household.py`), so the source can be swapped from local to Cuenta without touching goals or plans.**
- **Decided 2026-11-06 (ORCH-HOGAR-EN-CUENTA-1, contract `dev-protocol/contracts/hogar-v1.md`):** Cuenta holds who is in
  a household (consent from both, no cap of two, one active household per person, anyone can leave at any time).
  Body reads the `duna_hogar` claim `{id, miembros:[subs]}`. Body built `body/household.py` (local mirror of the
  claim) + `goal.hogar_id` + `plan_item.assignee_person_id`. Switching to the claim needs Body's Cuenta client:
  ask Main for CUENTA-API-CLIENTE-BODY-1 (with redirect URIs) when Body is hosted.
- Contacts: Finanzas orchestrator = session local_19b2985d-0843-4e85-b248-dc1ac667ce4a (direct). Lugares and Cuenta
  have no orchestrator: go through Main.

## vida-v1 at Body's boundaries (Main, 2026-11-06 · `dev-protocol/contracts/vida-v1.md`)

The contract governs what apps EXCHANGE, not Body's own tables. Body adopts it at two boundaries:
- **Out (done):** `python -m body.vida_export` writes `BODY_HOME/vida/body-week-<monday>.json`, the Review section
  `{area: "body", summary, metrics:[{key: "body.*", value, kind}], goals:[{goal_id, on_track}]}`. Every figure has
  visibility `owner`. Personal's Sunday review reads the file, never Body's database.
- **In (wave 4):** the shift import reads Personal's `turnos-*.json` as `work.shift` Events (FORMATO.md fields in
  `data`); the newest source wins day by day.
- When Body's goals/plans screens are built (wave 6), they follow vida-v1: goal `kind` metric|milestone with
  `direction`, `area`, status `proposed` (a coach suggestion, which counts only once he confirms it), habit + habit_check,
  structured cadence, goal_check per member, and a Plan `booking` where the final click is always his.

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

- 2026-11-06 — Main: "body" is a hub product in code (duna-orders 2ba3ff99, console 30c8a416, hub.py). Still Carlos's:
  deploy Pedidos, then mint a `body` caller key. Round 1 post ready in `docs/hub/round-1-para-productos.md`.
  Cuenta household live in code (cuenta-api ca41be4a, cuenta-ui a9df7537); Body switches `household.py` to the claim
  once it has its Cuenta client (CUENTA-API-CLIENTE-BODY-1, when hosted).

- 2026-11-06 — Starting direction accepted (Main): `python-garminconnect` locally now, official Health API later; shifts via Personal session.
- 2026-11-06 — Carlos: Hogar plan saved (own accounts, linked household, shared plans like a Kilimanjaro/El Cocuy
  prep). Body orchestrator may talk freely with other agents to coordinate.
- 2026-11-06 — Carlos: approved mock v1 direction. **Duna family look.** **Spanish (es-CO) default + full English
  version for him.** **Own readiness score** as the Hoy hero (not Body Battery). AI assistant like the other apps.
  Connections to the other apps (e.g. Finanzas). Start building and go deeper as we go.
- 2026-11-06 — Main: "body" joining the hub (ORDERS-HUB-PRODUCTO-BODY-1, ADMIN-HUB-PRODUCTO-BODY-1, hub.py PRODUCTS).
  Body can post once those deploy and Carlos mints a `body` caller key; then post DESIGN.md rules as para-productos
  and reply to existing rounds. "Duna personal" logged by Main as ORCH-BODY-HUB-AND-DUNA-PERSONAL-1 (horizon).
- 2026-11-06 — Carlos: start the serious design work on Duna principles; research → principles → scope → plan →
  mocks → build. Round 1 mock delivered (demo data). Idea logged: "Duna personal" horizon.
- 2026-11-06 — Main, under Carlos's trust: build slice 1 up to sign-in; responsive from day one (375 px), PC/LAN only;
  goals modelled generically with nothing personal seeded; shift schedule is a first-class input (`shift` table,
  `plan_item.shift_context`). Goal questions wait for Carlos.
