# HANDOFF — from the first Body session to the Body orchestrator

Written 2026-11-06. Carlos, as relayed by Main: "stop the current ones and start the new better ones". The new orchestrator is
`C:\Duna\body\dev-protocol`, and it dispatches workers into this repository. The project rules are in `CLAUDE.md`. The plan,
waves and decisions log are in `ROADMAP.md`, and the design rules in `docs/DESIGN.md`. This file adds what lived only in the
conversation.

## Where every piece stands (last commit 17a00c3; 12 tests pass)

`.venv\Scripts\python -m unittest discover tests -t .` runs the tests.

| Piece | State |
|---|---|
| Garmin data | Real data. Carlos signed in himself and the token is in `~/.body-app/garmin_tokens`. The full history is loaded: **871 days (2024-06-19 → 2026-11-06), 391 activities**. All 20 daily metrics are parsed, and only `body/garmin_adapter.py` imports garminconnect. |
| Pull | `python -m body.pull` handles `--date`, `--days N`, `--all` and `--recompute`. `--all` resumes from the oldest stored day and stops politely after repeated errors. Every pull ends with a readiness recompute. |
| Storage | SQLite at `~/.body-app/body.db` (`BODY_HOME`), outside the repo. The person, goal, plan, shift and metric tables are generic data. Migrations are in `db._migrate()`. |
| Baselines and readiness | Each day's baseline is the 30 days before it (at least 7 values). Readiness `body-v1` is our own estimate with weights hrv .30, sleep .25, battery .20, resting_hr .15 and yesterday's stress .10. It has bands and needs at least 3 parts, one of them hrv or sleep. Scored days: 843. |
| Web app | Runs with `python -m body.web [--lan] [--port]` (FastAPI + Jinja2). Screens: **Hoy** (readiness hero, "how I calculate it", 4 cards vs. your average), **Día** (prev/next, sleep stages, activities), **Tendencias** (History monthly line over your normal range, typical week, 7/30/90 daily lines vs. the period before, month → days → day drill-down, missing days break the line, night-shift shading ready), Ajustes (language). Turnos and Metas show a "coming soon" card. Bilingual from `body/i18n/es.json` and `en.json`; Carlos's profile is set to **en**. Checked at 375 px; light and dark; PWA. |
| Household | Lives behind `body/household.py` (local mirror of the hogar-v1 claim) + `goal.hogar_id` + `plan_item.assignee_person_id`. Cuenta's household is live in code (cuenta-api ca41be4a, cuenta-ui a9df7537). Switching needs Body's own Cuenta client (CUENTA-API-CLIENTE-BODY-1), to be requested when Body is hosted. |
| vida-v1 out | `python -m body.vida_export [--week MONDAY \| --current]` writes `~/.body-app/vida/body-week-<monday>.json` for Personal's Sunday review. Files exist for 2026-10-26 and 2026-11-02. |
| Connections | `docs/CONNECTIONS.md` agrees the link shape with Finanzas. Not shipped yet: the Finanzas link waits until his Finanzas Personal space exists. |
| Hub | "body" is in the code of duna-orders (2ba3ff99), the console (30c8a416) and `hub.py`. The round 1 post is written in `docs/hub/round-1-para-productos.md`, with its command. **No `~/.duna/hub-body.json` exists yet.** |
| Design | Research is in `docs/research/01–03`, the 17 rules in `docs/DESIGN.md`, and the round 1 mock in `design/mocks/v1/` (demo data). |

## Waiting on Carlos (exact wording as I asked him)

1. **The goal questions.** He has not answered them yet:
   1. "Which 1–3 health goals matter most to you right now? For example: sleep, resting heart rate, HRV, weight, fitness or energy."
   2. "Is there a number you'd like to reach, and by when?"
   3. "Around night shifts, what hurts most (sleep, energy, mood, training or food)? And what have you already tried?"
   4. "Are you training for something specific? Should the plans cover training, sleep timing, or both?"
2. **"Did "Kukui" mean El Cocuy? I assumed so."** Not confirmed.
3. **Design round 1.** He approved the direction: "Yes, I like it. Let's start with this and we go deeper." He has given no line-by-line notes on the mock, and he has not yet tested the built screens on his phone.
4. **Run `scripts\install-daily-sync.ps1` himself.** It installs "Body app daily sync" (09:30) and "Body app weekly file" (Sundays at 18:00). **Not installed**: `schtasks` shows no Body task, so data only arrives when someone runs the pull.
5. **Hub:** deploy Pedidos (it carries the "body" vocabulary), then mint a `body` caller key in the console. After that, the round 1 post can go out.

## Decisions that lived only in this conversation

- Readiness, not Body Battery, is the Hoy hero (Carlos's choice, against my recommendation). Language is es-CO by default with a full English version, and English is his own setting.
- Health values never appear in chat, logs or URLs. Jobs print counts only, and URLs carry only dates, ranges or months. The access log is off.
- **Tests are isolated:** `tests/_isolate.py` must be the first import in every test module. A test once wrote fake data into the real DB (2026-11-05); I repaired it. Keep the guard.
- Timezone is America/Bogota (the old Madrid value is migrated).
- Trends compares against the person's whole history (p25–p75 band). Hoy and Día compare against the 30-day baseline.
- In the week table, a lowest day is marked only when the weekday spread is at least 1 unit (5 minutes for sleep), so noise isn't highlighted.
- Night shifts are counted by `shift.day` = the day the shift starts (vida-v1 `work.shift`).
- I may talk directly with Main, Finanzas (local_19b2985d-0843-4e85-b248-dc1ac667ce4a), Lugares and Personal (memory `talk-freely-to-agents`). The household plan is to give Carlos and his girlfriend their own accounts, linked, with shared plans such as Kilimanjaro and El Cocuy (memory `shared-household-plans`).

## Next slices, in order

1. **Turnos import (wave 4):** read Personal's `C:\Personal\enel\turnos\turnos-*.json` (FORMATO.md fields, vida-v1 `work.shift`) into `shift`, with a one-tap confirm. The newest file wins day by day. This lights up the night shading already built in Tendencias. Personal will notify Body when its first file exists.
2. **Turnos screen:** a strip chart, night weeks vs. day weeks per metric, and ≈ days to recover with "how I calculate it".
3. **Metas (wave 6)** to vida-v1: goal kind metric|milestone, `proposed` status, habit + habit_check, weekly check in words, shift-context plan items. Seed his goals once he answers the four questions.
4. **Hub round 1:** once the key exists, post `docs/hub/round-1-para-productos.md` and reply to the existing `para_productos` items.
5. **Assistant (agent-ui-v1)** with a read tool per screen, then the Finanzas link once his Finanzas Personal space exists.

## Running processes and scheduled tasks

- A local web server, started in the background by this session: `.venv/Scripts/python -m body.web --port 8772` (127.0.0.1 only). It stops when this session is archived; restart it with that command.
- Scheduled tasks: **none installed** (see item 4 above).
