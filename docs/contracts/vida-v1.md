> Main owns this contract; the source is `C:\Duna\dev-protocol\contracts\vida-v1.md`. This is a read-only copy taken at FORK-1 (22f2b79e): re-copy it from the source, never edit it here.

# vida-v1 — the shared building blocks of Carlos's life coach

Main wrote this on 2026-11-06, as Carlos's Personal desk asked. His vision is in `C:\Personal\VISION.md`, posted to the
hub as hit_01m6rx9x0p7q5nvmzbxrhc9kc6. It starts from what exists: Personal's `enel/turnos/FORMATO.md`, Personal's
`vida/METAS.md`, and Body's `body/db.py`. It adopts Body's eight proposed changes.

**What this is.** One shape per block, so apps connect without being one app. It describes the shape of the data
EXCHANGED between apps: files, API payloads, and what one app reads from another. It does not dictate any app's own
tables. An app may store the data however it likes, as long as what it writes out and reads in has this shape.

**Where it applies now.** Personal (C:\Personal), Body (C:\Duna\body-app), and Finanzas for the money block. Other Duna
products adopt it only when they have a reason; the hub item tells them it exists.

## Common to every block

| Field        | Meaning |
|--------------|---------|
| `id`         | uuid, a shared id that other apps link to. An app keeps its own local int id beside it if it wants. |
| `person`     | Cuenta Duna `sub` when the person has one; until then, a local person key set by the app (for example `carlos`). |
| `hogar_id`   | hogar-v1 household id, or `null`. When set, the row belongs to the household, not only to `person`. |
| `visibility` | `owner` (the default) or `hogar`. Private unless the person chose to share. Health figures are never shared by default. |
| `source`     | WHO wrote it: `garmin`, `personal:<file>`, `body`, `finanzas`, `manual`, and so on. The same meaning everywhere. |
| `created_at` | ISO 8601 with offset. |

Dates are `YYYY-MM-DD`. A local time with no offset is `YYYY-MM-DDTHH:MM`, read in the row's `timezone` (default
`America/Bogota`). An instant is ISO 8601 with an offset.

## Metric — a number over time

* `metric_def`: `key` (namespaced: `body.resting_hr`, `finanzas.net_pay`, `vida.nature_days`), `label`, `unit`,
  `better` = `higher` | `lower` | `neutral`, `kind` = `measured` | `estimated` (an estimated value shows «≈» plus
  "cómo lo calculo" wherever it appears).
* `metric_value`: `person`, `day`, `key`, `value` (number), `source`, `visibility`.

## Event — something that happened or is scheduled

`id, person, hogar_id, kind, starts_at, ends_at, all_day, timezone, source, ref, data`

* `kind` is namespaced: `work.shift`, `trip.day` (Lugares), `money.payslip` (Finanzas), `body.workout`,
  `body.race`, and so on.
* `ref` is the source's own id (file + row, Lugares viaje id, Finanzas comprobante id).
* `data` holds the kind's own fields. For `work.shift` it holds EXACTLY Personal's FORMATO.md fields: `shift_kind` =
  `morning` | `afternoon` | `night` | `day` | `off` | `other`, `code` (the employer's own, unchanged), `holiday`, `sunday`,
  `note`. Here `starts_at`/`ends_at` are the shift's `start_local`/`end_local`, null when off, and the day is the day the
  shift STARTS. The newest source wins day by day. A shift carries no money.

## Goal — what someone wants, and why

`id, person, hogar_id, area, title, why, kind, metric_key, direction, target_value, baseline_value, start_day,
deadline, status, visibility`

* `area` = `body` | `money` | `adventure` | `growth` | `work` | `home`. The list is closed, and adding an area is a
  contract change.
* `kind` = `metric` (a target on a Metric: `metric_key` + `direction` = `raise` | `lower` | `keep` + `target_value`)
  or `milestone` (a dated outcome with no metric, for example "climb Kilimanjaro, Feb 2027"; `deadline` is set and
  `metric_key` is null).
* `status` = `proposed` | `active` | `paused` | `done` | `dropped`. **`proposed` is a goal the coach suggested and the
  person has not confirmed.** It never counts in a review until the person makes it `active`.

## Habit — something that repeats, and whether it was done

* `habit`: `id, goal_id, title, cadence, assignee, active`.
  `cadence` = `{"per": "day" | "week" | "month" | "quarter" | "year", "times": n, "context": "before_night" | "after_night" | "off_days" | null}`.
  `assignee` is a `person`, or null for every member of the goal's household.
* `habit_check`: `habit_id, person, day, done (bool), note`. This is where "done 5 of 7" comes from, and in a
  household each member has their own check.

## Plan — a dated thing to do

`id, person, hogar_id, goal_id (nullable), day, time (nullable), title, booking (nullable), status`

* `status` = `proposed` | `accepted` | `done` | `skipped`.
* `booking` = `{"needed": bool, "options": [{"what", "price", "currency", "url"}], "chosen": n | null}`. **The coach
  prepares the options; the final click that books or pays is always the person's.** No app books on its own.

## Review — the weekly page

* `review`: `person, week_start (a Monday), sections[], plans[] (Plan ids proposed for next week, max 3), created_at`.
  A section is `{"area", "summary", "metrics": [{key, value, kind}], "goals": [{goal_id, on_track: true | false | null}]}`.
* `goal_check`, one per member per goal per week: `goal_id, person, week_start, metric_avg, on_track, note`. This is
  Body's `weekly_check`, plus `person`.

## Rules

1. Personal data stays on the person's machine or Duna's own host. Product code ships no person's data.
2. An app reads another app's blocks through files or APIs in this shape, never by opening the other app's database.
3. A field a block does not list goes in `data` (Event) or waits for v2. Nobody adds a top-level field alone.
4. Changing this contract: Main writes v2 and posts it to the hub; v1 readers keep working. A purely ADDITIVE widening
   of a value list, where every old value keeps its meaning, stays in v1 and is logged below.

## Changelog

* 2026-11-06: cadence `per` gained `quarter` and `year` (Personal could not express its quarterly trip habit).
