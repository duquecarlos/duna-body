# HANDOFF — state of the Body app repository

This is Carlos's "human body" app: Garmin health data, shift correlation, goals and plans, and (soon) an AI
assistant, built as a template rather than hardcoded to one person. The Body orchestrator at
`C:\Duna\body\dev-protocol` dispatches slices into this repository. The project rules are in `CLAUDE.md`, the
plan (waves, decisions log, open questions) is in `ROADMAP.md`, the design rules are in `docs/DESIGN.md`, and the
full slice-by-slice history is the append-only `DECISIONS.md`. This file is state, not history — read the ledger
for *why*, this file for *what stands today*.

## Where every piece stands (main at `5a84e31`; 1121 tests)

`.venv\Scripts\python.exe -m unittest discover tests -t .` runs the tests.

| Piece | State |
|---|---|
| Garmin data & pull | Real data, Carlos's own sign-in token in `~/.body-app/garmin_tokens`; only `body/garmin_adapter.py` imports `garminconnect`. `python -m body.pull` takes `--date`, `--days N`, `--all` (resumes from the oldest stored day, stops politely on repeated errors) and `--recompute`; every pull ends with a readiness recompute. |
| Storage & migrations | SQLite at `~/.body-app/body.db` (`BODY_HOME`), outside the repo. `body/db.py`'s `SCHEMA` holds person, metric, sleep, activity, shift (+ `shift_batch`, `shift_pattern`), goal/habit/habit_check/goal_check/goal_enlace, plan, agent (`ajuste_agente`, `conversacion_agente`, `mensaje_agente`, `uso_agente`, `precio_modelo`, `tope_agente`, `propuesta_agente`), session/login (`sesion`, `inicio_sesion`, `logout_jti`), `consentimiento`, `garmin_cuenta`, `dispositivo` and `figura_compartida`. `_migrate()` carries every schema change since fork. |
| Baselines & readiness | 30-day baseline per metric (≥7 values). Readiness is version `body-v1` (hrv/sleep/battery/resting_hr/stress weights), banded, needs ≥3 parts with hrv or sleep among them. |
| Hoy (`/`) | Readiness hero with delta vs. baseline, 4 metric cards vs. average, today's due habits, the shift context for today, a household-shared-figures block, and "Primeros pasos" (`body/tutoriales.py`) for an unfinished onboarding checklist. |
| Día (`/day/{day}`), Cómo lo calculo (`/how/{day}`) | Prev/next day drill-down: metrics, sleep stages, activities, shift context; the readiness breakdown by part. |
| Tendencias (`/trends`), Historia (`/history`) | 7/30/90/whole-history switch vs. the period before, monthly line over the normal range, typical week, month → days → day drill-down, missing days break the line, night shifts shaded, by shift-kind comparison. |
| Turnos (`/shifts`, `/shifts/como`, `/shifts/patron`) | Built, and optional per person (`person.trabaja_turnos`, inferred `True` once shift data exists if still `NULL`; the whole Turnos nav item disappears for a person who has not said they work shifts). Import proposes batches from Personal's files for one-tap confirm (`/shifts/confirm`, `/shifts/confirm-all`, `/shifts/dismiss`); a cost/recovery hero (`body/turnos_costo.py`) shows what a night week costs and ≈ days to recover, with its own "cómo lo calculo"; `/api/turnos` lets Personal push shift batches directly. |
| Metas (`/goals`, `/goals/{id}`, `/goals/new`) | Built to vida-v1 shapes: goal `kind` metric\|milestone, `status` incl. `proposed`, habits with cadence (`{"per", "times"}`) and `assignee_person_id` **on `habit`, not on a `plan_item`** (there is no `plan_item` table — household assignment lives on the habit row), weekly `goal_check`, and `goal_enlace` (one link per app per goal, e.g. to a trip in Lugares via `body/enlaces.py`'s registry). Habit check-off posts from Hoy and from the goal page. |
| Assistant | `body/agente/`: a panel on every screen, `herramientas.py`'s static tool registry (read tools `leer_*` gated by `nivel_datos`, propose tools `proponer_meta`/`proponer_cambio_meta`/`proponer_confirmar_turnos`/`proponer_idioma` that only register a proposal — a person always confirms before anything changes), `cliente.py` (the only module that may call a model provider), `ajustes.py`/`uso.py`/`precios.py` for the per-person data level, usage log and spend ceiling. Providers: Anthropic, OpenAI, Gemini or a `demo` provider, chosen by `.env` (`BODY_AGENTE_*`, never committed). Conversation history and proposal apply/discard/undo are routed through `/asistente/*`. |
| Ajustes (`/settings`, `/ajustes/asistente`, `/ajustes/dispositivos`) | Language, sharing (`/settings/compartir`), the one-time "¿Trabajas por turnos?" question, the assistant's data level, and device management (`body/dispositivo.py`) for the hosted PC-connector key. |
| Primeros pasos / Tutoriales (`/ayuda`) | `body/tutoriales.py`'s catalog (`tutoriales/catalogo.json`) drives an onboarding checklist on Hoy and a help screen listing every chapter/task. |
| Hogar | `body/household.py` mirrors Cuenta's `duna_hogar` claim into `hogar_miembro` (rows `fuente='cuenta'` are the ONLY ones a sync from Cuenta writes; `fuente='local'` is demo/test data). **Body already has its own Cuenta client**: `body/cuenta.py` is a full OIDC relying party (`/login/cuenta/start`, `/login/cuenta/callback`, PKCE, JWKS, logout-token) — the earlier "needs CUENTA-API-CLIENTE-BODY-1" line no longer holds. A goal can belong to a household (`goal.hogar_id`) with per-member weekly checks; `body/compartir.py` shares one figure at a time with the household (withdrawable, `figura_compartida`), read by Hoy's shared-figures block. |
| Privacy | `/privacidad` (`body/privacidad.py`): export (`/privacidad/exportar`) and delete-all (`/privacidad/borrar`) walk every person-scoped table, including `habit`/`goal_enlace` via their `goal_id` join. Consent screen at `/privacidad/consentimiento`. No health figure in a URL or log; access logging is off by default (`body/request_log.py`). |
| vida-v1 boundaries | Out: `python -m body.vida_export [--week MONDAY \| --current]` writes `BODY_HOME/vida/body-week-<monday>.json` for Personal's Sunday review, now including shift counts and a recovery estimate. In: `body/shift_import.py` reads Personal's `turnos-*.json` (`FORMATO.md` fields) as `work.shift`, newest file wins per day. |
| Hosted mode | Real, not aspirational: `deploy/host/` holds systemd units (`body-web`, `body-pull`, `body-backfill`, `body-backup`) and timers, a `Caddyfile.body`, a deploy script and `body.env.example`. A PC can connect to the hosted server instead of running locally: `python -m body.conectar_pc` mints/pastes a device key, then `install-daily-sync.ps1 -Hosted` swaps the three local tasks for upload/fetch tasks. `body/listen.py` is the one place that decides `127.0.0.1` vs `0.0.0.0` (`--lan`). |
| Demo | `python -m body.demo` seeds one invented "Demo" person with ~90 days of invented figures, a rotating shift pattern and goals/habits, with a fixed seed for determinism; it refuses to touch any real or already-seeded `BODY_HOME`. Used by Growth for tutorial recordings, never real data. |

## Running processes

The local web app runs as a Windows scheduled task named **"Body app web"**, on port 8770 (local-only unless
installed with `-Lan`, or `-Hosted` for the server-connected mode). The orchestrator restarts it when needed — do
not query or touch it from a session here.

## What waits on Carlos

- His goal set: the four questions in `ROADMAP.md`'s "Open questions for Carlos" are still open, so no goal has
  been seeded from his own answers.
- The assistant's API key: paste one or more `BODY_AGENTE_*` lines into `.env` in this folder (never committed);
  without one, the assistant falls back to counting questions instead of a spend ceiling.
- `scripts\install-daily-sync.ps1` (or its `-Lan` / `-Hosted` form): installing the scheduled tasks is his hand step.
- Cuenta's deploy hand steps on the hosted path are Main's to walk him through, not this repository's.
- A git remote to push this repository to, if/when he wants one.

## Next

The orchestrator's queue (`C:\Duna\body\dev-protocol\queue\pending.txt`) is the only list of pending work — this
file does not keep a numbered next-slices list (it went stale once already). Open directions, in one line each:

- Links out to other Duna apps beyond Lugares (`body/enlaces.py`'s registry is built for more than one entry); the
  Finanzas link (wave 7b) waits on his Finanzas Personal space existing.
- Wave 8 (Duna product path — multi-person, Garmin Health API application) is his call, not scheduled.

## Contacts

The Body orchestrator may talk directly with Main, Finanzas (session `local_19b2985d-0843-4e85-b248-dc1ac667ce4a`)
and Personal; Lugares goes through Main (memory `talk-freely-to-agents`).
